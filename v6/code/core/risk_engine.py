"""Deterministic risk gate between the breakout signal and the executor.

Sizing: qty so that (qty · stop_distance) == equity · RISK_PER_TRADE, rounded
down to the symbol lot step. Gates (any failure → reject): manual kill switch,
concurrent-position cap, daily loss limit, high-water drawdown kill, min-qty.
No cooldown / streak / time-based rules by design.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from config.settings import RISK
from core.breakout_signal import EntryIntent

logger = logging.getLogger("metis.risk")


@dataclass
class SizedOrder:
    symbol: str
    side: str
    qty: float
    reference_price: float
    stop_price: float
    take_profit: float   # SHORT-only fixed R-target (0.0 for longs)
    leverage: int
    risk_usdt: float
    notional_usdt: float


@dataclass
class RiskDecision:
    ok: bool
    reason: Optional[str] = None
    order: Optional[SizedOrder] = None


def _round_step(qty: float, step: float, prec: int) -> float:
    if step <= 0:
        return round(qty, prec)
    return math.floor(qty / step) * step


def evaluate(intent: EntryIntent, *, equity: float, high_water: float,
             open_count: int, daily_realized: float, manual_kill: bool,
             specs: dict, max_concurrent: int = 0) -> RiskDecision:
    cap = max_concurrent or RISK.MAX_CONCURRENT  # 0 → 글로벌 기본(하위호환)
    if manual_kill:
        return RiskDecision(False, "manual_kill")
    if open_count >= cap:
        return RiskDecision(False, f"concurrent_cap:{open_count}>={cap}")
    if equity <= 0:
        return RiskDecision(False, "equity_nonpositive")
    if daily_realized <= -equity * RISK.DAILY_LOSS_LIMIT_PCT:
        return RiskDecision(False, f"daily_loss_limit:{daily_realized:.2f}")
    if high_water > 0 and (high_water - equity) / high_water >= RISK.MAX_DRAWDOWN_KILL_PCT:
        return RiskDecision(False, "max_drawdown_kill")

    if intent.stop_distance <= 0:
        return RiskDecision(False, "bad_stop_distance")
    risk_usdt = equity * RISK.RISK_PER_TRADE
    raw_qty = risk_usdt / intent.stop_distance
    sp = specs[intent.symbol]
    qty = _round_step(raw_qty, sp["qty_step"], sp["qty_precision"])
    if qty < sp["min_order_qty"]:
        return RiskDecision(False, f"below_min_qty:{qty}<{sp['min_order_qty']}")

    return RiskDecision(True, order=SizedOrder(
        symbol=intent.symbol, side=intent.side, qty=qty,
        reference_price=intent.reference_price, stop_price=intent.stop_price,
        take_profit=intent.take_profit,
        leverage=RISK.LEVERAGE, risk_usdt=qty * intent.stop_distance,
        notional_usdt=qty * intent.reference_price,
    ))
