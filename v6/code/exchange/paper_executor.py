"""Paper executor — simulates fills and books P&L against the local equity.

Entry fills at the reference price plus assumed slippage; both entry and exit
taker fees are subtracted so the recorded P&L equals the equity change exactly
(net-of-fees discipline). Stop/trend exits are decided by the engine, which
passes the exit price here for accounting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import RISK
from core.risk_engine import SizedOrder
from core.state_store import StateStore

logger = logging.getLogger("metis.paper")
_SLIP = RISK.SLIPPAGE_BPS_ASSUMED / 10000.0
_FEE = RISK.TAKER_FEE_PCT


class PaperExecutor:
    def __init__(self, store: StateStore):
        self.store = store

    async def open(self, o: SizedOrder) -> dict:
        d = 1 if o.side == "Buy" else -1
        entry = o.reference_price * (1 + d * _SLIP)
        fee = entry * o.qty * _FEE
        self.store.position_open(
            symbol=o.symbol, side=o.side, qty=o.qty, entry_price=entry,
            stop_price=o.stop_price, leverage=o.leverage,
            opened_utc=datetime.now(timezone.utc).isoformat(),
            entry_order_id="paper", sl_order_id="paper", take_profit=o.take_profit,
            ref_entry=o.reference_price)
        # entry fee reduces equity immediately
        eq, _ = self.store.equity_get()
        self.store.equity_set(eq - fee)
        self.store.journal("ENTER", o.symbol,
                           {"side": o.side, "qty": o.qty, "entry": entry, "stop": o.stop_price})
        return {"entry_price": entry, "fee": fee}

    async def close(self, pos: dict, exit_price: float, reason: str) -> float:
        d = 1 if pos["side"] == "Buy" else -1
        qty = pos["qty"]; entry = pos["entry_price"]
        gross = d * (exit_price - entry) * qty
        exit_fee = exit_price * qty * _FEE
        realized = gross - exit_fee            # entry fee already booked at open
        risk_usdt = abs(entry - pos["stop_price"]) * qty
        R = realized / risk_usdt if risk_usdt > 0 else 0.0
        eq, _ = self.store.equity_get()
        self.store.equity_set(eq + realized)
        self.store.trade_record(
            symbol=pos["symbol"], side=pos["side"], qty=qty, entry_price=entry,
            exit_price=exit_price, realized_usdt=realized, fees_usdt=exit_fee,
            realized_R=R, opened_utc=pos["opened_utc"],
            closed_utc=datetime.now(timezone.utc).isoformat(), close_reason=reason,
            ref_entry=pos.get("ref_entry", 0.0), ref_exit=exit_price)
        self.store.position_close(pos["symbol"])
        self.store.journal("EXIT", pos["symbol"], {"exit": exit_price, "reason": reason, "realized": realized, "R": R})
        return realized
