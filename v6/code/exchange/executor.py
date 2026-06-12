"""Live executor — places market entries on Bybit with an ATOMIC stop-loss, then
records the fill from authoritative exchange data.

Entry (open): balance guard → set leverage → ONE market order that already carries
the stop-loss (Bybit V5 lets a non-reduce-only order include stopLoss/tpslMode), so
the position is protected the instant it exists — no separate call, no naked window
even if the VM is slow or a follow-up request is lost. We then read the exact fill
(execPrice/execQty/execFee, summed over partial fills) from /v5/execution/list; if
that is still propagating we fall back to the position avgPrice, then to the
reference price. Every order has an orderLinkId so a retried request is idempotent.

Exit (close): the position may be closed by the bot (trend-end) or by the exchange
(server stop-loss / liquidation). close() sends the reduce-only market order only if
the position is still on the exchange, then books the trade from the authoritative
/v5/position/closed-pnl record (real exit price, realized P&L net of fees, fees) —
the same number the wallet reflects. The cycle/boot reconcile is the backstop: any
tracked position missing from the exchange is booked the same way.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from core.risk_engine import SizedOrder
from core.state_store import StateStore
from exchange.bybit_client import BybitClient

logger = logging.getLogger("metis.executor")

# Patient eventual-consistency polling — fills/closed-pnl propagate over a few
# seconds on a small VM; the atomic SL means there is no rush, only accuracy.
FILL_TRIES = 6
FILL_BACKOFF = [1.0, 1.5, 2.0, 2.5, 3.0, 3.0]
CLOSE_TRIES = 6
CLOSE_BACKOFF = [1.0, 1.5, 2.0, 2.5, 3.0, 3.0]


def _fmt(x: float, prec: int) -> str:
    return f"{x:.{prec}f}"


def _iso_to_ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except Exception:
        return 0


class LiveExecutor:
    def __init__(self, store: StateStore, bybit: BybitClient, specs: dict):
        self.store = store
        self.bybit = bybit
        self.specs = specs

    async def open(self, o: SizedOrder) -> dict:
        sp = self.specs[o.symbol]

        # balance guard — skip gracefully rather than firing a doomed order
        avail = await self.bybit.get_available_usdt()
        margin_need = o.notional_usdt / max(o.leverage, 1)
        if avail is not None and avail < margin_need * 1.05:
            logger.warning(f"skip {o.symbol}: margin {avail:.2f} < need {margin_need:.2f}")
            return {"ok": False, "error": "insufficient_margin"}

        await self.bybit.set_leverage(o.symbol, o.leverage)
        qty_s = _fmt(o.qty, sp["qty_precision"])
        prec = self._price_prec(sp)
        sl_s = _fmt(o.stop_price, prec)
        # shorts carry a fixed take-profit (2.5R), attached to the exchange so it fires
        # even if the bot is down; longs have take_profit 0.0 (trend exit, no fixed TP).
        tp_s = _fmt(o.take_profit, prec) if o.take_profit > 0 else None
        olid = f"m6-{o.symbol}-{int(time.time() * 1000)}"[:36]

        # entry + stop-loss (+ short take-profit) in one atomic order: if Bybit rejects
        # the SL/TP the whole order is rejected and we skip — never an unprotected position.
        res = await self.bybit.place_market(o.symbol, o.side, qty_s, reduce_only=False,
                                            stop_loss=sl_s, take_profit=tp_s, order_link_id=olid)
        rc = str(res.get("retCode"))
        dup = "orderlinkid" in str(res.get("retMsg", "")).lower()
        if rc != "0" and not dup:
            logger.error(f"entry failed {o.symbol}: {res.get('retMsg')}")
            return {"ok": False, "error": res.get("retMsg")}
        order_id = str((res.get("result") or {}).get("orderId", ""))

        entry_px, qty_fill, entry_fee = await self._lookup_fill(
            o.symbol, order_id, o.reference_price, o.qty)

        self.store.position_open(
            symbol=o.symbol, side=o.side, qty=qty_fill, entry_price=entry_px,
            stop_price=o.stop_price, leverage=o.leverage,
            opened_utc=datetime.now(timezone.utc).isoformat(),
            entry_order_id=order_id, sl_order_id=olid, take_profit=o.take_profit,
            ref_entry=o.reference_price)
        self.store.journal("ENTER", o.symbol, {
            "side": o.side, "qty": qty_fill, "entry": entry_px,
            "stop": o.stop_price, "take_profit": o.take_profit, "entry_fee": entry_fee})
        return {"ok": True, "entry_price": entry_px, "qty": qty_fill, "entry_fee": entry_fee}

    async def _lookup_fill(self, symbol: str, order_id: str, ref_px: float, ref_qty: float):
        """Exact entry from /v5/execution/list (sum partial fills) → (price, qty, fee).
        Falls back to the position avgPrice/size, then to the reference price."""
        for i in range(FILL_TRIES):
            await asyncio.sleep(FILL_BACKOFF[i])
            if order_id:
                try:
                    ex = await self.bybit.get_execution(symbol, order_id)
                except Exception:
                    ex = []
                tq = tv = fee = 0.0
                for e in ex:
                    q = float(e.get("execQty") or 0); px = float(e.get("execPrice") or 0)
                    tq += q; tv += q * px; fee += float(e.get("execFee") or 0)
                if tq > 0:
                    return tv / tq, tq, fee
            m = await self.bybit.get_positions_map()
            p = m.get(symbol)
            if p and float(p.get("size") or 0) > 0:
                return float(p.get("avgPrice") or ref_px), float(p.get("size") or ref_qty), 0.0
        logger.warning(f"{symbol} fill not yet visible; recording reference {ref_px} (reconcile will correct)")
        return ref_px, ref_qty, 0.0

    async def close(self, pos: dict, exit_price_hint: float, reason: str) -> float:
        """Close (or reconcile an already-closed) position; book it from closed-pnl."""
        sym = pos["symbol"]
        sp = self.specs[sym]
        m = await self.bybit.get_positions_map()
        live = m.get(sym)

        if live and float(live.get("size") or 0) > 0:
            close_side = "Sell" if pos["side"] == "Buy" else "Buy"
            qty_s = _fmt(float(live["size"]), sp["qty_precision"])
            olid = f"m6x-{sym}-{int(time.time() * 1000)}"[:36]
            res = await self.bybit.place_market(sym, close_side, qty_s, reduce_only=True,
                                                order_link_id=olid)
            rc = str(res.get("retCode"))
            # 110017 = reduce-only but the position already vanished (raced with the
            # server stop-loss) → fall through and book it from closed-pnl
            if rc != "0" and rc not in ("110017",):
                logger.error(f"close failed {sym}: {res.get('retMsg')}")
                return 0.0

        exit_price, realized, fees = await self._lookup_close(pos)
        if exit_price is None:
            d = 1 if pos["side"] == "Buy" else -1
            exit_price = exit_price_hint
            realized = d * (exit_price - pos["entry_price"]) * pos["qty"]
            fees = 0.0
            logger.warning(f"{sym} closed-pnl not found, using hint {exit_price}")

        # 'AUTO' = exchange closed it (server SL / TP / liquidation); label by P&L sign
        # so a short's take-profit isn't mislabeled as a stop-loss.
        if reason == "AUTO":
            reason = "TAKE_PROFIT" if realized > 0 else "STOP_LOSS"

        risk_usdt = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
        self.store.trade_record(
            symbol=sym, side=pos["side"], qty=pos["qty"], entry_price=pos["entry_price"],
            exit_price=exit_price, realized_usdt=realized, fees_usdt=fees,
            realized_R=realized / risk_usdt if risk_usdt > 0 else 0.0,
            opened_utc=pos["opened_utc"], closed_utc=datetime.now(timezone.utc).isoformat(),
            close_reason=reason, ref_entry=pos.get("ref_entry", 0.0), ref_exit=exit_price_hint)
        self.store.position_close(sym)
        self.store.journal("EXIT", sym, {"reason": reason, "realized": realized, "exit": exit_price, "fees": fees})
        return realized

    async def _lookup_close(self, pos: dict):
        """Authoritative close from Bybit → (exit_price, realized_usdt_net, total_fees).
        realized P&L and fees come from closed-pnl (Bybit-computed, exact). The exit
        PRICE comes from the execution fills (closing side, closedSize>0, averaged over
        partial fills) — exact, unlike closed-pnl avgExitPrice which is cost-based and
        diverges on partial fills. Every stored value matches Bybit's own record."""
        start_ms = _iso_to_ms(pos["opened_utc"]) - 60_000
        close_side = "Sell" if pos["side"] == "Buy" else "Buy"
        for i in range(CLOSE_TRIES):
            recs = await self.bybit.get_closed_pnl(pos["symbol"], start_ms)
            if recs:
                realized = sum(float(r.get("closedPnl") or 0) for r in recs)
                fees = sum(float(r.get("openFee") or 0) + float(r.get("closeFee") or 0) for r in recs)
                exit_px = None
                # exact exit price from execution fills (partial-fill aware)
                try:
                    ex = await self.bybit.get_executions(pos["symbol"], start_ms)
                    tq = tv = 0.0
                    for e in ex:
                        if e.get("side") == close_side and float(e.get("closedSize") or 0) > 0:
                            q = float(e.get("execQty") or 0)
                            tv += q * float(e.get("execPrice") or 0); tq += q
                    if tq > 0:
                        exit_px = tv / tq
                except Exception:
                    logger.exception(f"{pos['symbol']} exit execution lookup")
                if exit_px is None:  # fallback: closed-pnl avgExitPrice (cost-based)
                    tq = tv = 0.0
                    for r in recs:
                        q = float(r.get("qty") or 0)
                        tv += q * float(r.get("avgExitPrice") or 0); tq += q
                    exit_px = tv / tq if tq > 0 else None
                return exit_px, realized, fees
            await asyncio.sleep(CLOSE_BACKOFF[i])
        return None, 0.0, 0.0

    @staticmethod
    def _price_prec(sp: dict) -> int:
        ts = sp["tick_size"]
        s = f"{ts:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
