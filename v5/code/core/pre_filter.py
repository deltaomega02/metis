"""Pre-filter — cheap deterministic checks before the LLM call.

Runs after the FeatureSnapshot is built and before the Gemini call. Skipping
out here avoids burning a Flash call (and its latency budget) on cycles that
are doomed by data quality, event windows, an already-open position, or the
risk kill/cooldown switches.

Failures map to one of the schema's critical reject enums where applicable;
informational blocks (position open, manual kill, cooldown) leave the field
null and are surfaced via ``reason`` only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config.settings import CYCLE
from core.feature_builder import FeatureSnapshot

logger = logging.getLogger("metis.pre_filter")


@dataclass
class PreFilterResult:
    """Outcome of one pre-filter pass."""

    pass_through: bool                      # True → proceed to the LLM. False → skip and force NO_TRADE.
    reason: Optional[str]
    forced_critical_reject: Optional[str]   # One of the schema reject enums, when the block maps to one.


def evaluate(
    snap: FeatureSnapshot,
    position_open: bool,
    risk_state: dict,
    now_utc: Optional[datetime] = None,
) -> PreFilterResult:
    """Pre-LLM gate. Returns ``pass_through=False`` when the cycle should be skipped."""
    now_utc = now_utc or datetime.now(timezone.utc)

    # 1. Data quality
    if not snap.data_quality_ok:
        return PreFilterResult(
            pass_through=False,
            reason=f"data_quality_failed:{','.join(snap.quality_notes[:3])}",
            forced_critical_reject="DATA_QUALITY_FAIL",
        )

    # 2. Event filter — BLOCK skips the LLM entirely. REDUCE_ONLY proceeds:
    # the prompt rules require the LLM to output NO_TRADE with "reduce-only"
    # cited in reason_short rather than treating it as a hard reject.
    ev_state = snap.event_filter.get("state", "CLEAR")
    if ev_state == "BLOCK":
        return PreFilterResult(
            pass_through=False,
            reason=f"event_block:{snap.event_filter.get('reason', 'unknown')}",
            forced_critical_reject="EVENT_FILTER_BLOCK",
        )

    # 3. Already in a position — winner-takes-all means at most one open trade.
    if position_open:
        return PreFilterResult(
            pass_through=False,
            reason="position_already_open",
            forced_critical_reject=None,  # informational, not a schema reject
        )

    # 4. Risk kill switch
    kill_until = risk_state.get("kill_until_utc")
    if kill_until:
        try:
            kt = datetime.fromisoformat(kill_until.replace("Z", "+00:00"))
            if now_utc < kt:
                return PreFilterResult(
                    pass_through=False,
                    reason=f"risk_kill_until:{kill_until}",
                    forced_critical_reject="LOSS_STREAK_STOP",
                )
        except (TypeError, ValueError):
            pass

    if risk_state.get("manual_kill", 0):
        return PreFilterResult(
            pass_through=False,
            reason="manual_kill_active",
            forced_critical_reject=None,
        )

    cooldown_until = risk_state.get("cooldown_until_utc")
    if cooldown_until:
        try:
            ct = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
            if now_utc < ct:
                return PreFilterResult(
                    pass_through=False,
                    reason=f"cooldown_until:{cooldown_until}",
                    forced_critical_reject=None,
                )
        except (TypeError, ValueError):
            pass

    return PreFilterResult(pass_through=True, reason=None, forced_critical_reject=None)
