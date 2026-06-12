"""Context asset features + macro/event filter.

Two responsibilities:

1. ``compute_context_asset`` summarises a peer major (BTC + the other one of
   SOL/ETH) on the 1h timeframe so the decision engine can see whether the
   broader market is aligned, neutral, against, or in a shock move.
2. ``evaluate_event_filter`` reads ``events.yaml`` and returns CLEAR /
   REDUCE_ONLY / BLOCK based on scheduled high-impact macro events and any
   configured recurring windows (daily reset, funding settlement, ...). A
   missing or stale calendar conservatively blocks entries.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import yaml  # PyYAML
except ImportError:
    yaml = None  # Without PyYAML we cannot load events.yaml; the filter will block conservatively.

from config.settings import EVENT_YAML_PATH


# ──────────────────────────────────────────────────────────────────────
# Context asset feature
# ──────────────────────────────────────────────────────────────────────
@dataclass
class ContextAssetFeature:
    """1h-timeframe summary of one peer asset (BTC, ETH, ...)."""

    symbol: str
    ret_1h_pct: float
    ret_4h_pct: float
    ema20_vs_ema50_state: str  # "bullish" | "bearish" | "neutral"
    rsi14: float
    adx14: float
    atr14_pct: float
    market_shock_flag: bool


def compute_context_asset(
    symbol: str,
    ret_1h_pct: float,
    ret_4h_pct: float,
    ema20: float,
    ema50: float,
    rsi14: float,
    adx14: float,
    atr14_pct: float,
    last_close: float,
    *,
    shock_atr_multiple: float = 3.0,
) -> ContextAssetFeature:
    """Assemble the 7-field context summary.

    ``market_shock_flag`` is set when the absolute 1h return exceeds
    ``shock_atr_multiple`` × ATR%, which catches sharp dislocations that
    invalidate normal entry conditions.
    """
    state = "neutral"
    if math.isfinite(ema20) and math.isfinite(ema50):
        if ema20 > ema50 * 1.001:
            state = "bullish"
        elif ema20 < ema50 * 0.999:
            state = "bearish"

    shock = False
    if math.isfinite(ret_1h_pct) and math.isfinite(atr14_pct) and atr14_pct > 0:
        shock = abs(ret_1h_pct) > shock_atr_multiple * atr14_pct

    return ContextAssetFeature(
        symbol=symbol,
        ret_1h_pct=round(ret_1h_pct, 3),
        ret_4h_pct=round(ret_4h_pct, 3),
        ema20_vs_ema50_state=state,
        rsi14=round(rsi14, 2),
        adx14=round(adx14, 2),
        atr14_pct=round(atr14_pct, 3),
        market_shock_flag=shock,
    )


# ──────────────────────────────────────────────────────────────────────
# Event filter
# ──────────────────────────────────────────────────────────────────────
@dataclass
class EventFilterResult:
    state: str               # "CLEAR" | "REDUCE_ONLY" | "BLOCK"
    reason: Optional[str]
    minutes_to_next_high_impact: Optional[int]
    size_multiplier: float   # 1.0 = full, <1.0 reduce
    yaml_stale: bool
    kst09_window: bool       # in the UTC23:55-UTC00:30 window


@dataclass
class EventCalendar:
    last_updated_utc: datetime
    events: list[dict] = field(default_factory=list)
    recurring_windows: list[dict] = field(default_factory=list)


def _load_yaml(path: Path = EVENT_YAML_PATH) -> Optional[dict]:
    if yaml is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def load_event_calendar(path: Path = EVENT_YAML_PATH) -> Optional[EventCalendar]:
    data = _load_yaml(path)
    if data is None:
        return None
    try:
        last_updated = datetime.fromisoformat(data["last_updated_utc"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return EventCalendar(
        last_updated_utc=last_updated,
        events=data.get("events", []) or [],
        recurring_windows=data.get("recurring_windows", []) or [],
    )


def evaluate_event_filter(
    now_utc: Optional[datetime] = None,
    calendar: Optional[EventCalendar] = None,
    stale_days: int = 7,
) -> EventFilterResult:
    """Return BLOCK / REDUCE_ONLY / CLEAR based on calendar."""
    now_utc = now_utc or datetime.now(timezone.utc)

    if calendar is None:
        calendar = load_event_calendar()
    if calendar is None:
        # No calendar available — fail safe by blocking new entries.
        return EventFilterResult(
            state="BLOCK", reason="event_calendar_missing",
            minutes_to_next_high_impact=None, size_multiplier=0.0,
            yaml_stale=True, kst09_window=False,
        )

    # stale check
    stale = (now_utc - calendar.last_updated_utc) > timedelta(days=stale_days)
    if stale:
        return EventFilterResult(
            state="BLOCK", reason=f"event_calendar_stale_{(now_utc - calendar.last_updated_utc).days}d",
            minutes_to_next_high_impact=None, size_multiplier=0.0,
            yaml_stale=True, kst09_window=False,
        )

    # recurring windows
    state: str = "CLEAR"
    size_mult = 1.0
    reason: Optional[str] = None
    kst09_window = False

    now_hhmm = now_utc.strftime("%H:%M")
    for win in calendar.recurring_windows:
        if not win.get("enabled", False):
            continue
        name = win.get("name", "")
        # daily reset window
        if "block_start_utc_hhmm" in win and "block_end_utc_hhmm" in win:
            bstart = win["block_start_utc_hhmm"]
            bend = win["block_end_utc_hhmm"]
            in_block = _in_hhmm_window(now_hhmm, bstart, bend)
            if in_block:
                state = "BLOCK"
                reason = f"recurring_{name}"
                size_mult = 0.0
                if "kst09" in name:
                    kst09_window = True
                break
            rstart = win.get("reduce_only_start_utc_hhmm")
            rend = win.get("reduce_only_end_utc_hhmm")
            if rstart and rend and _in_hhmm_window(now_hhmm, rstart, rend):
                if state == "CLEAR":
                    state = "REDUCE_ONLY"
                    reason = f"recurring_{name}"
                    size_mult = 0.5
                if "kst09" in name:
                    kst09_window = True
        # funding settlement
        if "times_utc" in win:
            before = int(win.get("block_before_min", 5))
            after = int(win.get("block_after_min", 5))
            for hhmm in win["times_utc"]:
                in_fund = _within_minutes_of(now_utc, hhmm, before, after)
                if in_fund and state == "CLEAR":
                    state = "REDUCE_ONLY"
                    reason = f"funding_settle_{hhmm}"
                    size_mult = min(size_mult, 0.5)

    # one-off events
    minutes_to_next_high = None
    for ev in calendar.events:
        try:
            start_dt = datetime.fromisoformat(ev["start_utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        impact = ev.get("impact", "low")
        before = int(ev.get("block_before_min", 0))
        after = int(ev.get("block_after_min", 0))
        size = float(ev.get("size_multiplier", 1.0))

        delta_min = int((start_dt - now_utc).total_seconds() / 60)
        # high-impact upcoming?
        if impact == "high" and 0 < delta_min < 24 * 60:
            if minutes_to_next_high is None or delta_min < minutes_to_next_high:
                minutes_to_next_high = delta_min

        win_start = start_dt - timedelta(minutes=before)
        win_end = start_dt + timedelta(minutes=after)
        if win_start <= now_utc <= win_end:
            # full block during the immediate window for high-impact
            if impact == "high":
                state = "BLOCK"
                reason = f"event_{ev.get('event_id', impact)}"
                size_mult = 0.0
            else:
                if state == "CLEAR":
                    state = "REDUCE_ONLY"
                    reason = f"event_{ev.get('event_id', impact)}"
                    size_mult = min(size_mult, size)

    return EventFilterResult(
        state=state, reason=reason,
        minutes_to_next_high_impact=minutes_to_next_high,
        size_multiplier=size_mult, yaml_stale=False, kst09_window=kst09_window,
    )


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_hhmm_window(now_hhmm: str, start_hhmm: str, end_hhmm: str) -> bool:
    """Return True if ``now`` falls inside the [start, end] HH:MM window.

    Wraps over midnight when start > end (e.g. 23:55 → 00:10).
    """
    now = _parse_hhmm(now_hhmm)
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def _within_minutes_of(now_utc: datetime, hhmm: str, before_min: int, after_min: int) -> bool:
    t = _parse_hhmm(hhmm)
    target_today = now_utc.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    candidates = [target_today, target_today - timedelta(days=1), target_today + timedelta(days=1)]
    for c in candidates:
        delta = abs((now_utc - c).total_seconds() / 60)
        if c <= now_utc:
            # past target
            if delta <= after_min:
                return True
        else:
            # future target
            if delta <= before_min:
                return True
    return False
