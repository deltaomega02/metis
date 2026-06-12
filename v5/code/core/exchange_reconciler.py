"""Exchange reconciler — keeps local state in sync with Bybit.

Boot reconcile (``reconcile_at_startup``):
- Aborts any stale ``cycle_lock`` rows so a crashed previous process does not
  block new cycles.
- Walks stale PREPARED/SUBMITTED orders and resolves them against the exchange.
- Adopts open positions back into local state, or — when a live position is
  unprotected — emergency-closes it (we cannot infer SL/TP after the fact).

Periodic reconcile (``reconcile_periodic``):
- Flags any drift between local state and exchange state every 5 minutes,
  without auto-closing. Drift writes a journal row for the operator to inspect.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from ai.prompt_registry import SCHEMA_VERSION
from config.settings import PAPER_MODE, TRADING
from core.order_journal import get_order_journal
from core.state_store import EventRecord, get_state_store
from exchange.bybit_client import BybitError, get_bybit_client
from exchange.paper_executor import get_paper_executor

logger = logging.getLogger("metis.reconciler")


class ExchangeReconciler:
    """Compares local state against the exchange and repairs drift."""

    def __init__(self):
        self.bybit = get_bybit_client()
        self.paper = get_paper_executor() if PAPER_MODE else None
        self.oj = get_order_journal()
        self.store = get_state_store()

    async def reconcile_at_startup(self) -> dict:
        """Boot reconcile: adopt orphan positions, or emergency-close them if unprotected."""
        logger.info("Startup reconciliation...")
        report = {"adopted": [], "orphans_closed": [], "stale_locks": 0, "stale_orders": 0}

        # 1) abort stale cycle locks
        self.store.cycle_lock_abort_stale(max_age_sec=300)
        report["stale_locks"] = 1

        # 2) stale PREPARED/SUBMITTED orders → query exchange
        stale = self.oj.find_stale_prepared(older_than_sec=60)
        report["stale_orders"] = len(stale)
        for o in stale:
            try:
                if PAPER_MODE:
                    self.oj.mark_cancelled(o["client_order_id"], "paper_stale")
                    continue
                # try lookup by client_order_id
                opens = await self.bybit.get_open_orders(symbol=o["symbol"])
                found = None
                for x in opens:
                    if x.get("orderLinkId") == o["client_order_id"]:
                        found = x
                        break
                if found:
                    # exchange still has it open — cancel
                    await self.bybit.cancel_order(o["symbol"], client_order_id=o["client_order_id"])
                self.oj.mark_cancelled(o["client_order_id"], "startup_stale")
            except Exception as e:
                logger.warning(f"stale order reconcile failed for {o['client_order_id']}: {e}")

        # 3) open positions
        if PAPER_MODE:
            pp = self.paper.get_open_position()
            local = self.store.snapshot_get("open_position") or {}
            local_status = local.get("status")
            if pp and local_status != "OPEN":
                logger.warning(f"paper position exists in DB but state missing — adopting")
                self.store.snapshot_set("open_position", {
                    "status": "OPEN",
                    "position_id": pp["position_id"],
                    "symbol": pp["symbol"],
                    "side": pp["side"],
                    "qty": pp["qty"],
                    "entry_price": pp["entry_price"],
                    "stop_loss": pp["stop_loss"],
                    "take_profit": pp["take_profit"],
                    "leverage": pp["leverage"],
                    "opened_utc": pp["opened_utc"],
                    "adopted_from_db": True,
                })
                report["adopted"].append(pp["symbol"])
            elif not pp and local_status == "OPEN":
                logger.warning("state says OPEN but paper has no position — clearing")
                self.store.snapshot_set("open_position", {"status": "NONE"})
        else:
            # live
            for sym in TRADING.SYMBOLS:
                try:
                    positions = await self.bybit.get_positions(symbol=sym)
                    open_positions = [p for p in positions if float(p.get("size", 0)) > 0]
                    local = self.store.snapshot_get("open_position") or {}
                    if open_positions:
                        # orphan: live position with no protection or no local record
                        p = open_positions[0]
                        sl = float(p.get("stopLoss", 0) or 0)
                        tp = float(p.get("takeProfit", 0) or 0)
                        if sl <= 0 or tp <= 0:
                            # unprotected — safe mode: cannot decide TP/SL post-hoc safely
                            await self._safe_mode_close(sym, p)
                            report["orphans_closed"].append(sym)
                        elif local.get("status") != "OPEN" or local.get("symbol") != sym:
                            # protected — adopt
                            logger.info(f"adopting orphan protected {sym}")
                            self.store.snapshot_set("open_position", {
                                "status": "OPEN",
                                "position_id": p.get("positionIdx", "orphan"),
                                "symbol": sym,
                                "side": "Buy" if p.get("side") == "Buy" else "Sell",
                                "qty": float(p.get("size", 0)),
                                "entry_price": float(p.get("avgPrice", 0)),
                                "stop_loss": sl,
                                "take_profit": tp,
                                "leverage": int(float(p.get("leverage", 1))),
                                "opened_utc": datetime.now(timezone.utc).isoformat(),
                                "adopted_orphan": True,
                            })
                            report["adopted"].append(sym)
                except Exception as e:
                    logger.warning(f"position reconcile failed for {sym}: {e}")

        self._journal("RECONCILE", "startup", report)
        logger.info(f"reconcile report: {report}")
        return report

    async def reconcile_periodic(self) -> dict:
        """Periodic drift check. Flags inconsistencies without auto-closing."""
        if PAPER_MODE:
            return {"skipped": "paper"}
        drift = []
        for sym in TRADING.SYMBOLS:
            try:
                positions = await self.bybit.get_positions(symbol=sym)
                exchange_open = any(float(p.get("size", 0)) > 0 for p in positions)
                local = self.store.snapshot_get("open_position") or {}
                local_open = local.get("status") == "OPEN" and local.get("symbol") == sym
                if exchange_open != local_open:
                    drift.append({"symbol": sym, "exchange_open": exchange_open, "local_open": local_open})
            except Exception as e:
                logger.warning(f"periodic reconcile {sym}: {e}")
        if drift:
            logger.warning(f"reconcile drift: {drift}")
            self._journal("RECONCILE_DRIFT", "periodic", {"drift": drift})
        return {"drift": drift}

    async def _safe_mode_close(self, symbol: str, position: dict):
        side = position.get("side")
        qty = float(position.get("size", 0))
        opposite = "Sell" if side == "Buy" else "Buy"
        try:
            await self.bybit.place_order(
                symbol=symbol, side=opposite, order_type="Market",
                qty=qty, client_order_id=f"m4-emc-orph-{symbol}",
                reduce_only=True, time_in_force="IOC",
            )
            logger.warning(f"orphan unprotected {symbol} {side} {qty} → emergency closed")
        except Exception as e:
            logger.exception(f"safe-mode close failed: {e}")

    def _journal(self, kind: str, scope: str, payload: dict):
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=scope, kind=kind, symbol=None, actor="RECONCILER",
                prompt_hash=None, schema_version=SCHEMA_VERSION,
                risk_config_hash=None, payload=payload,
            ))
        except Exception:
            logger.exception("journal append failed")
