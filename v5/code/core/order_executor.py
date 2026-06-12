"""Order executor — entry placement + protection-order attach.

Each ENTER decision walks through a deterministic state machine:

  1. Risk engine PASS (handled by the caller).
  2. ``order_journal.prepare`` allocates a deterministic ``client_order_id``.
  3. ``mark_submitted`` → exchange ``place_order`` (market).
  4. ``mark_acked`` + ``mark_filled`` with avg fill price + fees.
  5. **Immediately** attach SL/TP via ``set_trading_stop`` (live) or store on
     the paper row (paper). The protection-attach delay budget is enforced
     hard — if it is exceeded the position is closed reduce-only.
  6. ``snapshot_set('open_position', ...)`` records the open position so the
     monitor and the next cycle can see it.

Paper mode reuses the same flow but calls ``PaperExecutor`` instead of Bybit.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ai.prompt_registry import SCHEMA_VERSION
from ai.response_schema import MetisDecision
from config.settings import PAPER_MODE, RISK, TRADING
from core.order_journal import OrderIntent, get_order_journal
from core.risk_engine import SizedOrder
from core.state_store import EventRecord, get_state_store
from exchange.bybit_client import BybitError, get_bybit_client
from exchange.paper_executor import get_paper_executor

logger = logging.getLogger("metis.order_executor")


@dataclass
class ExecutionResult:
    """Outcome of one entry attempt."""

    success: bool
    position_id: Optional[str]
    decision_id: str
    fill_price: Optional[float]
    qty: Optional[float]
    fees_usdt: float
    error: Optional[str]
    protection_attached: bool
    protection_attach_delay_sec: float


class OrderExecutor:
    """Entry placer + protection-order attach. Single-flight by design."""

    def __init__(self):
        self.bybit = get_bybit_client()
        self.paper = get_paper_executor() if PAPER_MODE else None
        self.oj = get_order_journal()
        self.store = get_state_store()

    async def execute_entry(
        self,
        decision: MetisDecision,
        sized: SizedOrder,
        cycle_id: str,
        prompt_hash: str,
    ) -> ExecutionResult:
        """Submit the entry order, immediately attach SL/TP, and record the open position."""
        decision_id = f"d_{cycle_id}_{decision.symbol}"
        side = sized.side
        symbol = sized.symbol

        # ── Step 1: register the intent in the order journal (idempotent) ──
        entry_intent = OrderIntent(
            decision_id=decision_id,
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=sized.qty,
            price=None,
            reduce_only=False,
            purpose="ENTRY",
            notes={
                "reference_price": sized.reference_price,
                "invalidation_price": sized.invalidation_price,
                "target_price": sized.target_price,
                "leverage": sized.leverage,
                "decision": decision.decision,
                "setup_id": decision.setup_id,
            },
        )
        entry_coid = self.oj.prepare(entry_intent)

        # ── Step 2: submit the entry order ──
        self.oj.mark_submitted(entry_coid)
        t_start = time.time()
        fill_price: Optional[float] = None
        qty_filled: Optional[float] = None
        fees: float = 0.0
        position_id: Optional[str] = None

        try:
            if PAPER_MODE:
                # paper fill
                position_id = f"p_{decision_id}"
                pf = self.paper.open_position(
                    position_id=position_id,
                    decision_id=decision_id,
                    symbol=symbol,
                    side=side,
                    qty=sized.qty,
                    reference_price=sized.reference_price,
                    leverage=sized.leverage,
                    stop_loss=None,
                    take_profit=None,
                )
                fill_price = pf.fill_price
                qty_filled = pf.qty
                fees = pf.fees_usdt
                self.oj.mark_acked(entry_coid, exchange_order_id=position_id)
                self.oj.mark_filled(entry_coid, filled_qty=qty_filled, avg_fill_price=fill_price, fees_usdt=fees)
            else:
                # live
                try:
                    await self.bybit.set_leverage(symbol, sized.leverage)
                except BybitError as e:
                    logger.warning(f"set_leverage warn: {e}")
                resp = await self.bybit.place_order(
                    symbol=symbol, side=side, order_type="Market",
                    qty=sized.qty, client_order_id=entry_coid,
                    time_in_force="IOC",
                )
                ex_id = resp.get("result", {}).get("orderId", "")
                self.oj.mark_acked(entry_coid, exchange_order_id=ex_id)

                # Resolve fill (poll execution list briefly)
                fill_info = await self._poll_fill(symbol, entry_coid, timeout_sec=10)
                if fill_info:
                    fill_price = fill_info["avg_price"]
                    qty_filled = fill_info["qty"]
                    fees = fill_info["fees"]
                    self.oj.mark_filled(entry_coid, filled_qty=qty_filled, avg_fill_price=fill_price, fees_usdt=fees)
                else:
                    self.oj.mark_rejected(entry_coid, "fill_not_resolved")
                    return ExecutionResult(success=False, position_id=None, decision_id=decision_id,
                                           fill_price=None, qty=None, fees_usdt=0.0,
                                           error="fill_not_resolved", protection_attached=False,
                                           protection_attach_delay_sec=0.0)
                position_id = ex_id or entry_coid

        except Exception as e:
            self.oj.mark_rejected(entry_coid, str(e)[:200])
            logger.exception(f"entry execute failed: {e}")
            return ExecutionResult(success=False, position_id=None, decision_id=decision_id,
                                   fill_price=None, qty=None, fees_usdt=0.0,
                                   error=str(e)[:200], protection_attached=False,
                                   protection_attach_delay_sec=0.0)

        # ── Step 3: attach protection orders (CRITICAL) ──
        # SL/TP must be in place before the next price tick can move against
        # the position. Exceeding PROTECTION_ORDER_MAX_DELAY_SEC is a hard
        # violation and triggers an emergency reduce-only close below.
        attach_t0 = time.time()
        protection_ok = False
        try:
            protection_ok = await self._attach_protection(
                symbol=symbol,
                side=side,
                stop_loss=sized.invalidation_price,
                take_profit=sized.target_price,
                position_id=position_id,
                decision_id=decision_id,
                cycle_id=cycle_id,
            )
        except Exception as e:
            logger.exception(f"protection attach failed: {e}")
            protection_ok = False

        attach_delay = time.time() - attach_t0
        total_delay = time.time() - t_start

        # If the attach failed or took too long, do not leave an unprotected
        # position on the book. Emergency reduce-only close.
        if not protection_ok or total_delay > RISK.PROTECTION_ORDER_MAX_DELAY_SEC:
            logger.error(
                f"PROTECTION FAILURE: position {position_id} unprotected after "
                f"{total_delay:.1f}s — emergency close"
            )
            await self._emergency_close(symbol, side, qty_filled or sized.qty, position_id, decision_id, cycle_id)
            self._journal("PROTECTION_FAILURE", symbol, cycle_id, prompt_hash,
                          f"unprotected_for_{total_delay:.1f}s; emergency_closed")
            return ExecutionResult(
                success=False, position_id=position_id, decision_id=decision_id,
                fill_price=fill_price, qty=qty_filled, fees_usdt=fees,
                error="protection_failure_emergency_close",
                protection_attached=False, protection_attach_delay_sec=attach_delay,
            )

        # ── Step 4: record the open position in state_snapshot ──
        opened_at_iso = datetime.now(timezone.utc).isoformat()
        self.store.snapshot_set("open_position", {
            "status": "OPEN",
            "position_id": position_id,
            "decision_id": decision_id,
            "cycle_id": cycle_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_filled,
            "entry_price": fill_price,
            "stop_loss": sized.invalidation_price,
            "take_profit": sized.target_price,
            "leverage": sized.leverage,
            "opened_utc": opened_at_iso,
            "setup_id": decision.setup_id,
            "confidence": decision.confidence,
            "fees_usdt_entry": fees,
        })
        self._journal("POSITION_OPEN", symbol, cycle_id, prompt_hash,
                      f"qty={qty_filled} fill={fill_price} SL={sized.invalidation_price} TP={sized.target_price}")

        return ExecutionResult(
            success=True, position_id=position_id, decision_id=decision_id,
            fill_price=fill_price, qty=qty_filled, fees_usdt=fees,
            error=None, protection_attached=True, protection_attach_delay_sec=attach_delay,
        )

    # ── protection-order attach (live + paper) ──
    async def _attach_protection(
        self,
        symbol: str,
        side: str,
        stop_loss: float,
        take_profit: float,
        position_id: str,
        decision_id: str,
        cycle_id: str,
    ) -> bool:
        # Paper: just persist on the paper_position row.
        if PAPER_MODE:
            self.paper.set_protection(position_id, stop_loss, take_profit)
            return True
        # Live: exchange-native trading-stop with one retry on transient error.
        for attempt in range(2):
            try:
                await self.bybit.set_trading_stop(
                    symbol=symbol, stop_loss=stop_loss, take_profit=take_profit,
                )
                return True
            except Exception as e:
                logger.warning(f"set_trading_stop attempt {attempt+1} failed: {e}")
                await asyncio.sleep(0.5)
        return False

    async def _emergency_close(
        self, symbol: str, side: str, qty: float, position_id: str,
        decision_id: str, cycle_id: str,
    ):
        opposite = "Sell" if side == "Buy" else "Buy"
        try:
            if PAPER_MODE:
                ticker = await self.bybit.get_ticker(symbol)
                mark = float(ticker.get("markPrice") or ticker.get("lastPrice") or 0)
                if mark > 0:
                    self.paper.close_position(position_id, mark, "EMERGENCY_PROTECTION_FAIL")
            else:
                close_coid = f"m4-emc-{decision_id[:16]}"
                await self.bybit.place_order(
                    symbol=symbol, side=opposite, order_type="Market",
                    qty=qty, client_order_id=close_coid,
                    reduce_only=True, time_in_force="IOC",
                )
        except Exception as e:
            logger.exception(f"emergency_close failed: {e}")

    async def _poll_fill(self, symbol: str, coid: str, timeout_sec: int = 10) -> Optional[dict]:
        end = time.time() + timeout_sec
        while time.time() < end:
            try:
                execs = await self.bybit.get_executions(symbol=symbol, limit=20)
                related = [e for e in execs if e.get("orderLinkId") == coid]
                if related:
                    qty = sum(float(e.get("execQty", 0)) for e in related)
                    if qty > 0:
                        notional = sum(
                            float(e.get("execQty", 0)) * float(e.get("execPrice", 0))
                            for e in related
                        )
                        fees = sum(float(e.get("execFee", 0)) for e in related)
                        return {"qty": qty, "avg_price": notional / qty, "fees": fees}
            except Exception as e:
                logger.debug(f"poll_fill warn: {e}")
            await asyncio.sleep(0.5)
        return None

    def _journal(self, kind: str, symbol: str, cycle_id: str, prompt_hash: str, detail: str):
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id, kind=kind, symbol=symbol, actor="EXECUTOR",
                prompt_hash=prompt_hash, schema_version=SCHEMA_VERSION,
                risk_config_hash=None, payload={"detail": detail},
            ))
        except Exception:
            logger.exception("journal append failed")


_executor: Optional[OrderExecutor] = None


def get_order_executor() -> OrderExecutor:
    global _executor
    if _executor is None:
        _executor = OrderExecutor()
    return _executor
