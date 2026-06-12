"""Gemini response contract (Pydantic v2).

Defines the JSON schema that the LLM must return and enforces every
cross-field invariant via ``model_validator``.

Design notes:
- ``schema_version`` is a frozen literal — the registry hash depends on it.
- Enums (decision, setup_id, regime, rejects, ...) are typed as Literal so
  Pydantic + Gemini's response_json_schema reject any unknown value.
- ``ConfigDict(extra='forbid')`` rejects unknown fields outright; this catches
  prompt drift and hallucinated keys at parse time.
- The ENTER path requires consistent prices (LONG: inv < ref < tgt) and a
  declared target_R that matches the actual reward / risk within 0.15.
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── enums ──────────────────────────────────────────────────────────────
Symbol = Literal["SOLUSDT", "ETHUSDT"]
Decision = Literal["ENTER_LONG", "ENTER_SHORT", "NO_TRADE"]
SetupId = Literal[
    "TREND_PULLBACK",
    "BREAKOUT_RETEST",
    "RANGE_REVERSION",
    "FUNDING_OI_SQUEEZE",
    "LIQUIDATION_SWEEP_REVERSAL",
    "NONE",
]
Regime = Literal["trend_up", "trend_down", "range", "high_vol", "low_vol"]
VolatilityRegime = Literal["high", "normal", "low"]
EntryType = Literal["MARKET_ON_CYCLE_CLOSE", "LIMIT_RETEST", "STOP_BREAKOUT", "NONE"]
Direction = Literal["LONG", "SHORT", "NONE"]
OverrideAction = Literal["KEEP", "DOWNGRADE", "VETO_NO_TRADE"]
EventFilterState = Literal["CLEAR", "REDUCE_ONLY", "BLOCK"]
ContextState = Literal["aligned", "neutral", "against", "shock"]

Reject = Literal[
    "DATA_QUALITY_FAIL",
    "EVENT_FILTER_BLOCK",
    "BTC_ETH_CONTEXT_CONFLICT",
    "REGIME_SETUP_MISMATCH",
    "VOLATILITY_UNTRADABLE",
    "LIQUIDITY_SPREAD_BAD",
    "FUNDING_OI_NOT_CONFIRMING",
    "TAKER_FLOW_CONFLICT",
    "NO_CLEAR_LEVEL",
    "NO_STRUCTURAL_INVALIDATION",
    "RR_BELOW_MIN",
    "ENTRY_CHASING_OR_LATE",
    "RANGE_LOCATION_BAD",
    "LIQUIDATION_CONTEXT_ABSENT",
    "MULTI_SIGNAL_CONFLICT",
]

Short120 = Annotated[str, Field(max_length=120)]
Short160 = Annotated[str, Field(max_length=160)]
Short220 = Annotated[str, Field(max_length=220)]


# ── nested models ──────────────────────────────────────────────────────
class EntryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_type: EntryType
    reference_price: Optional[float] = Field(default=None, gt=0)
    entry_zone_low: Optional[float] = Field(default=None, gt=0)
    entry_zone_high: Optional[float] = Field(default=None, gt=0)
    signal_valid_minutes: int = Field(ge=0, le=30)
    max_slippage_bps: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_zone(self) -> "EntryPlan":
        if self.entry_zone_low is not None and self.entry_zone_high is not None:
            if self.entry_zone_low > self.entry_zone_high:
                raise ValueError("entry_zone_low cannot exceed entry_zone_high")
        return self


class TechnicalProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_decision: Decision
    setup_id: SetupId
    direction: Direction
    entry_ref: Optional[float] = Field(default=None, gt=0)
    invalidation_ref: Optional[float] = Field(default=None, gt=0)
    target_ref: Optional[float] = Field(default=None, gt=0)
    confidence_raw: float = Field(ge=0.0, le=0.95)
    technical_evidence_summary: Short220


class SupervisorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_candidate: Decision
    opposing_evidence: List[Short120] = Field(default_factory=list, max_length=3)
    critical_rejects: List[Reject] = Field(default_factory=list, max_length=3)
    override_action: OverrideAction
    supervisor_opposing_evidence: Short220


# ── top-level decision ─────────────────────────────────────────────────
class MetisDecision(BaseModel):
    """Top-level decision contract returned by Gemini Flash for one symbol."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["metis_v4_r5_freeze_v1"]
    symbol: Symbol
    asof_utc: str = Field(min_length=10)
    input_snapshot_id: str = Field(min_length=1)

    decision: Decision
    setup_id: SetupId
    confidence: float = Field(ge=0.0, le=0.95)
    regime: Regime
    volatility_regime: VolatilityRegime
    entry_plan: EntryPlan

    structural_invalidation_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    target_R: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    # AI-decided leverage (1..5). Bounded by RISK.LEVERAGE_MAX in risk_engine.
    leverage: Optional[int] = Field(default=None, ge=1, le=5)
    # time_stop_minutes deprecated — schema accepts the field but the engine ignores it.
    time_stop_minutes: Optional[int] = Field(default=None, ge=0, le=10080)

    technical_proposal: TechnicalProposal
    supervisor_review: SupervisorReview

    critical_reject_matched: List[Reject] = Field(default_factory=list, max_length=3)
    supervisor_check_passed: bool
    event_filter_state: EventFilterState
    btc_eth_context_state: ContextState
    data_quality_ok: bool

    reason_short: Short160

    # ── cross-field invariants ──
    @model_validator(mode="after")
    def check_trade_invariants(self) -> "MetisDecision":
        ep = self.entry_plan
        if self.decision == "NO_TRADE":
            if self.setup_id != "NONE" or ep.entry_type != "NONE":
                raise ValueError("NO_TRADE must use setup_id NONE and entry_type NONE")
            blocked = [
                ep.reference_price,
                ep.entry_zone_low,
                ep.entry_zone_high,
                ep.max_slippage_bps,
                self.structural_invalidation_price,
                self.target_price,
                self.target_R,
                self.leverage,
            ]
            if any(x is not None for x in blocked):
                raise ValueError("NO_TRADE must null all trade plan prices/R/leverage")
            if ep.signal_valid_minutes != 0:
                raise ValueError("NO_TRADE must use signal_valid_minutes 0")
            return self

        # ── ENTER path ──
        if self.confidence < 0.75:
            raise ValueError("ENTER requires confidence >= 0.75")
        if not self.supervisor_check_passed:
            raise ValueError("ENTER requires supervisor_check_passed = true")
        if self.critical_reject_matched or self.supervisor_review.critical_rejects:
            raise ValueError("ENTER requires empty critical_rejects")
        if not self.data_quality_ok:
            raise ValueError("ENTER requires data_quality_ok = true")
        if self.event_filter_state != "CLEAR":
            raise ValueError("ENTER requires event_filter_state = CLEAR")
        if self.btc_eth_context_state in ("against", "shock"):
            raise ValueError("ENTER cannot pass with btc_eth_context_state against/shock")
        if self.setup_id == "NONE" or ep.entry_type == "NONE":
            raise ValueError("ENTER requires non-NONE setup and entry_type")

        required = [
            ep.reference_price,
            self.structural_invalidation_price,
            self.target_price,
            self.target_R,
        ]
        if any(x is None for x in required):
            raise ValueError(
                "ENTER must have reference, invalidation, target, target_R"
            )
        if ep.signal_valid_minutes <= 0:
            raise ValueError("ENTER requires positive signal_valid_minutes")

        ref = float(ep.reference_price)
        inv = float(self.structural_invalidation_price)
        tgt = float(self.target_price)
        tr = float(self.target_R)

        if self.decision == "ENTER_LONG" and not (inv < ref < tgt):
            raise ValueError("LONG requires invalidation < reference < target")
        if self.decision == "ENTER_SHORT" and not (tgt < ref < inv):
            raise ValueError("SHORT requires target < reference < invalidation")

        risk = abs(ref - inv)
        reward = abs(tgt - ref)
        if risk <= 0:
            raise ValueError("risk distance must be positive")
        calc_r = reward / risk
        if abs(calc_r - tr) > 0.15:
            raise ValueError(f"target_R mismatch (declared {tr:.2f}, calc {calc_r:.2f}) exceeds 0.15")
        if tr < 1.2:
            raise ValueError("ENTER requires target_R >= 1.2")

        return self


# ── Gemini responseJsonSchema adapter ──
def gemini_response_schema_dict() -> dict:
    """Return the Pydantic JSON schema in a form Gemini accepts.

    The Gemini API does not honor `pattern`/regex or deeply nested `$defs`, so
    we lightly post-process the schema. Currently we only strip the top-level
    title; unsupported keys are silently ignored by the API.
    """
    schema = MetisDecision.model_json_schema()
    schema.pop("title", None)
    return schema
