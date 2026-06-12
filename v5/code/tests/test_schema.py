"""Contract tests for the response schema — Pydantic invariants and gates."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from pydantic import ValidationError

from ai.prompt_registry import SCHEMA_VERSION
from ai.response_schema import MetisDecision


# The schema_version literal in MetisDecision is frozen — tests must use the
# same string the registry publishes.
SCHEMA = SCHEMA_VERSION


def _no_trade(**overrides) -> dict:
    base = dict(
        schema_version=SCHEMA, symbol="SOLUSDT",
        asof_utc="2026-05-28T12:00:00Z", input_snapshot_id="s1",
        decision="NO_TRADE", setup_id="NONE", confidence=0.0,
        regime="range", volatility_regime="normal",
        entry_plan={"entry_type": "NONE", "signal_valid_minutes": 0},
        technical_proposal={
            "candidate_decision": "NO_TRADE", "setup_id": "NONE", "direction": "NONE",
            "confidence_raw": 0.3, "technical_evidence_summary": "테스트",
        },
        supervisor_review={
            "reviewed_candidate": "NO_TRADE", "override_action": "VETO_NO_TRADE",
            "supervisor_opposing_evidence": "테스트", "critical_rejects": ["RANGE_LOCATION_BAD"],
        },
        critical_reject_matched=["RANGE_LOCATION_BAD"],
        supervisor_check_passed=False,
        event_filter_state="CLEAR", btc_eth_context_state="neutral",
        data_quality_ok=True, reason_short="NO_TRADE 테스트",
    )
    base.update(overrides)
    return base


def _enter_long(**overrides) -> dict:
    base = dict(
        schema_version=SCHEMA, symbol="SOLUSDT",
        asof_utc="2026-05-28T12:00:00Z", input_snapshot_id="s2",
        decision="ENTER_LONG", setup_id="TREND_PULLBACK", confidence=0.80,
        regime="trend_up", volatility_regime="normal",
        entry_plan={"entry_type": "MARKET_ON_CYCLE_CLOSE",
                    "reference_price": 150.0, "signal_valid_minutes": 15},
        structural_invalidation_price=148.5, target_price=152.4,
        target_R=1.6, time_stop_minutes=90,
        technical_proposal={
            "candidate_decision": "ENTER_LONG", "setup_id": "TREND_PULLBACK", "direction": "LONG",
            "entry_ref": 150.0, "invalidation_ref": 148.5, "target_ref": 152.4,
            "confidence_raw": 0.80, "technical_evidence_summary": "1H/4H 상승, 풀백",
        },
        supervisor_review={
            "reviewed_candidate": "ENTER_LONG", "opposing_evidence": ["타이트 SL"],
            "override_action": "KEEP", "supervisor_opposing_evidence": "BTC 횡보",
        },
        supervisor_check_passed=True,
        event_filter_state="CLEAR", btc_eth_context_state="aligned",
        data_quality_ok=True, reason_short="TREND_PULLBACK LONG",
    )
    base.update(overrides)
    return base


# ── Validation: positive ──
def test_no_trade_valid():
    MetisDecision(**_no_trade())


def test_enter_long_valid():
    MetisDecision(**_enter_long())


def test_enter_short_valid():
    d = _enter_long(
        decision="ENTER_SHORT",
        entry_plan={"entry_type": "MARKET_ON_CYCLE_CLOSE",
                    "reference_price": 150.0, "signal_valid_minutes": 15},
        structural_invalidation_price=151.5,
        target_price=147.6,
        target_R=1.6,
        technical_proposal={
            "candidate_decision": "ENTER_SHORT", "setup_id": "TREND_PULLBACK", "direction": "SHORT",
            "entry_ref": 150.0, "invalidation_ref": 151.5, "target_ref": 147.6,
            "confidence_raw": 0.80, "technical_evidence_summary": "1H/4H 하락",
        },
        supervisor_review={
            "reviewed_candidate": "ENTER_SHORT", "opposing_evidence": ["BTC 신고가"],
            "override_action": "KEEP", "supervisor_opposing_evidence": "BTC 강세",
        },
    )
    MetisDecision(**d)


# ── NO_TRADE field nulling ──
def test_no_trade_must_null_invalidation():
    bad = _no_trade(structural_invalidation_price=150.0)
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_no_trade_must_null_target():
    bad = _no_trade(target_price=151.0)
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_no_trade_setup_none():
    bad = _no_trade(setup_id="TREND_PULLBACK")
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


# ── ENTER gates ──
def test_enter_confidence_below_075():
    bad = _enter_long(confidence=0.74)
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_enter_requires_supervisor_pass():
    bad = _enter_long(supervisor_check_passed=False)
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_enter_cannot_carry_rejects():
    bad = _enter_long(critical_reject_matched=["VOLATILITY_UNTRADABLE"])
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_enter_event_must_be_clear():
    bad = _enter_long(event_filter_state="BLOCK")
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_enter_context_against_blocked():
    bad = _enter_long(btc_eth_context_state="against")
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_enter_context_shock_blocked():
    bad = _enter_long(btc_eth_context_state="shock")
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


# ── Price invariants ──
def test_long_invalidation_must_be_below_ref():
    bad = _enter_long(structural_invalidation_price=152.0)  # inv > ref
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_long_target_must_be_above_ref():
    bad = _enter_long(target_price=149.0)  # tgt < ref
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_short_invalidation_must_be_above_ref():
    d = _enter_long(
        decision="ENTER_SHORT",
        structural_invalidation_price=149.0,  # inv < ref (violation for SHORT)
        target_price=147.0,
        target_R=1.5,
        technical_proposal={
            "candidate_decision": "ENTER_SHORT", "setup_id": "TREND_PULLBACK", "direction": "SHORT",
            "entry_ref": 150.0, "invalidation_ref": 149.0, "target_ref": 147.0,
            "confidence_raw": 0.80, "technical_evidence_summary": "test",
        },
        supervisor_review={
            "reviewed_candidate": "ENTER_SHORT", "opposing_evidence": ["x"],
            "override_action": "KEEP", "supervisor_opposing_evidence": "x",
        },
    )
    with pytest.raises(ValidationError):
        MetisDecision(**d)


# ── target_R consistency ──
def test_target_r_must_match_prices():
    bad = _enter_long(target_R=2.5)  # ref=150 inv=148.5 tgt=152.4 → calc 1.6 ≠ 2.5
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


def test_target_r_must_be_min_12():
    bad = _enter_long(
        target_price=151.5, target_R=1.0,
        technical_proposal={
            "candidate_decision": "ENTER_LONG", "setup_id": "TREND_PULLBACK", "direction": "LONG",
            "entry_ref": 150.0, "invalidation_ref": 148.5, "target_ref": 151.5,
            "confidence_raw": 0.80, "technical_evidence_summary": "x",
        },
        supervisor_review={
            "reviewed_candidate": "ENTER_LONG", "opposing_evidence": ["x"],
            "override_action": "KEEP", "supervisor_opposing_evidence": "x",
        },
    )
    with pytest.raises(ValidationError):
        MetisDecision(**bad)


# ── Extra fields forbidden ──
def test_extra_field_forbidden():
    bad = _no_trade()
    bad["unknown_extra"] = 123
    with pytest.raises(ValidationError):
        MetisDecision(**bad)
