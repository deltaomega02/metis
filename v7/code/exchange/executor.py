"""Live executor — places market entries on Bybit and attaches an exchange-held
stop-loss so the position is protected even if the bot goes down.

Entry: set leverage → market order → set full-position stop-loss (reduce-only,
last-price trigger). Exit (trend-end, engine-decided): reduce-only market close.
Equity is read from the wallet by the engine; realized P&L here is a best-effort
record using the last traded price.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import TRADING
from core.risk_engine import SizedOrder
from core.state_store import StateStore
from exchange.bybit_client import BybitClient

logger = logging.getLogger("metis.executor")


def _fmt(x: float, prec: int) -> str:
    return f"{x:.{prec}f}"


class LiveExecutor:
    def __init__(self, store: StateStore, bybit: BybitClient, specs: dict):
        self.store = store
        self.bybit = bybit
        self.specs = specs

    async def open(self, o: SizedOrder) -> dict:
        sp = self.specs[o.symbol]
        await self.bybit.set_leverage(o.symbol, o.leverage)
        qty_s = _fmt(o.qty, sp["qty_precision"])
        res = await self.bybit.place_market(o.symbol, o.side, qty_s, reduce_only=False)
        if str(res.get("retCode")) != "0":
            logger.error(f"entry failed {o.symbol}: {res.get('retMsg')}")
            return {"ok": False, "error": res.get("retMsg")}
        sl_s = _fmt(o.stop_price, self._price_prec(sp))
        sl_res = await self.bybit.set_stop_loss(o.symbol, sl_s)
        self.store.position_open(
            symbol=o.symbol, side=o.side, qty=o.qty, entry_price=o.reference_price,
            stop_price=o.stop_price, leverage=o.leverage,
            opened_utc=datetime.now(timezone.utc).isoformat(),
            entry_order_id=str((res.get("result") or {}).get("orderId", "")),
            sl_order_id=str(sl_res.get("retCode")))
        self.store.journal("ENTER", o.symbol, {"side": o.side, "qty": o.qty, "stop": o.stop_price})
        return {"ok": True, "entry_price": o.reference_price}

    async def close(self, pos: dict, exit_price: float, reason: str) -> float:
        close_side = "Sell" if pos["side"] == "Buy" else "Buy"
        sp = self.specs[pos["symbol"]]
        res = await self.bybit.place_market(pos["symbol"], close_side,
                                            _fmt(pos["qty"], sp["qty_precision"]), reduce_only=True)
        if str(res.get("retCode")) != "0":
            logger.error(f"close failed {pos['symbol']}: {res.get('retMsg')}")
            return 0.0
        d = 1 if pos["side"] == "Buy" else -1
        realized = d * (exit_price - pos["entry_price"]) * pos["qty"]
        risk_usdt = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
        self.store.trade_record(
            symbol=pos["symbol"], side=pos["side"], qty=pos["qty"], entry_price=pos["entry_price"],
            exit_price=exit_price, realized_usdt=realized, fees_usdt=0.0,
            realized_R=realized / risk_usdt if risk_usdt > 0 else 0.0,
            opened_utc=pos["opened_utc"], closed_utc=datetime.now(timezone.utc).isoformat(),
            close_reason=reason)
        self.store.position_close(pos["symbol"])
        self.store.journal("EXIT", pos["symbol"], {"reason": reason, "realized": realized})
        return realized

    @staticmethod
    def _price_prec(sp: dict) -> int:
        ts = sp["tick_size"]
        s = f"{ts:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
