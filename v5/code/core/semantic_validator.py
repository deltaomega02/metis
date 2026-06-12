"""Semantic validator — runtime price/RR sanity beyond the Pydantic schema.

The response schema handles every cross-field invariant the LLM can violate on
its own (price ordering, target_R consistency, NO_TRADE field nulling, ...).
This module adds the runtime checks that need the live market context:

- Round the proposed prices to the symbol's tick.
- Confirm the SL distance still sits inside the absolute % bounds and the
  ATR-multiple band after rounding.
- Confirm the reference price has not drifted too far from the latest mark.
- Reject spreads above ``MAX_SPREAD_BPS`` when a top-of-book snapshot is present.
- Re-check the price-order invariant on the rounded values (rounding can flip
  it when the levels were tight).

The result also carries the tick-rounded prices that the risk engine and the
executor downstream use.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from ai.response_schema import MetisDecision
from config.settings import RISK, TRADING

logger = logging.getLogger("metis.semantic_validator")


@dataclass
class SemanticResult:
    """Outcome of one validation call."""

    ok: bool
    violations: list[str] = field(default_factory=list)
    # Tick-rounded prices (populated only for ENTER decisions).
    normalized_reference_price: Optional[float] = None
    normalized_invalidation_price: Optional[float] = None
    normalized_target_price: Optional[float] = None
    sl_distance_pct: Optional[float] = None
    tp_distance_pct: Optional[float] = None


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(price / tick) * tick


def validate(
    decision: MetisDecision,
    last_price: float,
    atr_15m_pct: float,
    spread_bps: Optional[float] = None,
) -> SemanticResult:
    """Validate an ENTER decision against live market context.

    NO_TRADE always passes through. For ENTER, prices are rounded to the
    symbol's tick and then checked for distance bounds, ATR-multiple sanity,
    reference drift vs ``last_price``, spread width, and direction invariants.
    Returns a ``SemanticResult`` with the rounded prices when validation passes.
    """
    if decision.decision == "NO_TRADE":
        return SemanticResult(ok=True)

    violations: list[str] = []
    specs = TRADING.symbol_specs.get(decision.symbol)
    if not specs:
        return SemanticResult(ok=False, violations=[f"unknown_symbol:{decision.symbol}"])

    tick = float(specs["tick_size"])
    ref = float(decision.entry_plan.reference_price)
    inv = float(decision.structural_invalidation_price)
    tgt = float(decision.target_price)

    # tick-round
    ref_r = _round_to_tick(ref, tick)
    inv_r = _round_to_tick(inv, tick)
    tgt_r = _round_to_tick(tgt, tick)

    # SL distance pct
    sl_pct = abs(ref_r - inv_r) / ref_r if ref_r > 0 else float("nan")
    tp_pct = abs(tgt_r - ref_r) / ref_r if ref_r > 0 else float("nan")

    if not math.isfinite(sl_pct) or sl_pct < RISK.SL_DISTANCE_MIN_PCT:
        violations.append(f"sl_too_tight:{sl_pct*100:.3f}%<{RISK.SL_DISTANCE_MIN_PCT*100:.2f}%")
    if sl_pct > RISK.SL_DISTANCE_MAX_PCT:
        violations.append(f"sl_too_wide:{sl_pct*100:.3f}%>{RISK.SL_DISTANCE_MAX_PCT*100:.2f}%")

    # SL distance must sit inside the [SL_ATR_MIN, SL_ATR_MAX] × ATR band —
    # tighter is whip risk, wider is bad RR.
    if math.isfinite(atr_15m_pct) and atr_15m_pct > 0:
        ratio = sl_pct / (atr_15m_pct / 100)
        if ratio < RISK.SL_ATR_MIN:
            violations.append(f"sl_atr_ratio_low:{ratio:.2f}<{RISK.SL_ATR_MIN}")
        if ratio > RISK.SL_ATR_MAX:
            violations.append(f"sl_atr_ratio_high:{ratio:.2f}>{RISK.SL_ATR_MAX}")

    # Reference price must not have drifted more than 0.5% from the live mark
    # while the cycle was processing — otherwise the entry is chasing.
    if last_price > 0:
        drift = abs(ref_r - last_price) / last_price
        if drift > 0.005:
            violations.append(f"reference_drift_too_far:{drift*100:.3f}%>0.5%")

    # Spread check
    if spread_bps is not None and math.isfinite(spread_bps):
        if spread_bps > RISK.MAX_SPREAD_BPS:
            violations.append(f"spread_too_wide:{spread_bps:.2f}bps>{RISK.MAX_SPREAD_BPS}bps")

    # Rounding to the tick can flip the strict inequality on a tight level;
    # re-check the direction invariant on the rounded prices.
    if decision.decision == "ENTER_LONG":
        if not (inv_r < ref_r < tgt_r):
            violations.append("rounded_prices_violate_long_invariant")
    elif decision.decision == "ENTER_SHORT":
        if not (tgt_r < ref_r < inv_r):
            violations.append("rounded_prices_violate_short_invariant")

    # Re-check declared target_R against the rounded prices; tolerance is
    # slightly looser than the Pydantic 0.15 because rounding can shift R.
    risk_amt = abs(ref_r - inv_r)
    reward_amt = abs(tgt_r - ref_r)
    if risk_amt > 0:
        calc_r = reward_amt / risk_amt
        if abs(calc_r - float(decision.target_R)) > 0.20:
            violations.append(
                f"target_R_drift_after_rounding:declared={decision.target_R:.2f},calc={calc_r:.2f}"
            )

    return SemanticResult(
        ok=len(violations) == 0,
        violations=violations,
        normalized_reference_price=ref_r,
        normalized_invalidation_price=inv_r,
        normalized_target_price=tgt_r,
        sl_distance_pct=sl_pct,
        tp_distance_pct=tp_pct,
    )
