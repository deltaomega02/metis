"""Structural level extractor.

Derives the T2 features from a 15m OHLCV series:

- 24h / 3d high+low and the ATR-normalised distance from the last close.
- Swing high+low over the most recent N bars (default 20).
- Session AVWAP (since UTC 00:00 of the current day) plus distance and
  side flag (above / below VWAP).

All distances are expressed in ATR multiples so the LLM can reason about
proximity to structure independently of price absolute scale.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.technical_indicators import OHLCV


@dataclass
class T2Levels:
    """Structural levels with ATR-normalised distances (distance / ATR)."""

    # 24h
    high_24h: float
    low_24h: float
    dist_24h_high_atr: float
    dist_24h_low_atr: float
    # 3d
    high_3d: float
    low_3d: float
    dist_3d_high_atr: float
    dist_3d_low_atr: float
    # swing (last N=20 bars on the timeframe)
    swing_high_n20: float
    swing_low_n20: float
    dist_swing_high_atr: float
    dist_swing_low_atr: float
    # session VWAP (since UTC 00:00 of current day)
    session_vwap: float
    dist_session_vwap_atr: float
    above_session_vwap: bool


def _safe_div(num: float, den: float) -> float:
    return num / den if math.isfinite(den) and den > 0 else float("nan")


def compute_levels(
    ohlcv_15m: OHLCV,
    atr_15m_last: float,
    bars_24h: int = 96,    # 96 × 15m = 24h
    bars_3d: int = 288,    # 288 × 15m = 72h
    swing_window: int = 20,
) -> T2Levels:
    """Compute structural levels from a 15m series. ``atr_15m_last`` normalises distances."""
    n = len(ohlcv_15m)
    if n == 0 or not math.isfinite(atr_15m_last) or atr_15m_last <= 0:
        nan = float("nan")
        return T2Levels(
            high_24h=nan, low_24h=nan, dist_24h_high_atr=nan, dist_24h_low_atr=nan,
            high_3d=nan, low_3d=nan, dist_3d_high_atr=nan, dist_3d_low_atr=nan,
            swing_high_n20=nan, swing_low_n20=nan,
            dist_swing_high_atr=nan, dist_swing_low_atr=nan,
            session_vwap=nan, dist_session_vwap_atr=nan, above_session_vwap=False,
        )

    last_close = float(ohlcv_15m.close[-1])

    def _slice_hi_lo(slice_size: int) -> tuple[float, float]:
        if n < slice_size:
            slice_size = n
        sl_high = float(ohlcv_15m.high[-slice_size:].max())
        sl_low = float(ohlcv_15m.low[-slice_size:].min())
        return sl_high, sl_low

    h_24, l_24 = _slice_hi_lo(bars_24h)
    h_3d, l_3d = _slice_hi_lo(bars_3d)
    h_sw, l_sw = _slice_hi_lo(swing_window)

    # Session AVWAP over the bars whose open is at or after today's UTC 00:00.
    last_ts_ms = int(ohlcv_15m.ts_ms[-1])
    last_dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
    session_start_dt = last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    session_start_ms = int(session_start_dt.timestamp() * 1000)
    session_mask = ohlcv_15m.ts_ms >= session_start_ms
    if session_mask.any():
        s_close = ohlcv_15m.close[session_mask]
        s_high = ohlcv_15m.high[session_mask]
        s_low = ohlcv_15m.low[session_mask]
        s_vol = ohlcv_15m.volume[session_mask]
        typical = (s_high + s_low + s_close) / 3.0
        wsum = float(np.sum(typical * s_vol))
        vsum = float(np.sum(s_vol))
        vwap = wsum / vsum if vsum > 0 else float("nan")
    else:
        vwap = float("nan")

    return T2Levels(
        high_24h=round(h_24, 6),
        low_24h=round(l_24, 6),
        dist_24h_high_atr=round(_safe_div(h_24 - last_close, atr_15m_last), 3),
        dist_24h_low_atr=round(_safe_div(last_close - l_24, atr_15m_last), 3),
        high_3d=round(h_3d, 6),
        low_3d=round(l_3d, 6),
        dist_3d_high_atr=round(_safe_div(h_3d - last_close, atr_15m_last), 3),
        dist_3d_low_atr=round(_safe_div(last_close - l_3d, atr_15m_last), 3),
        swing_high_n20=round(h_sw, 6),
        swing_low_n20=round(l_sw, 6),
        dist_swing_high_atr=round(_safe_div(h_sw - last_close, atr_15m_last), 3),
        dist_swing_low_atr=round(_safe_div(last_close - l_sw, atr_15m_last), 3),
        session_vwap=round(vwap, 6) if math.isfinite(vwap) else float("nan"),
        dist_session_vwap_atr=round(_safe_div(last_close - vwap, atr_15m_last), 3) if math.isfinite(vwap) else float("nan"),
        above_session_vwap=last_close > vwap if math.isfinite(vwap) else False,
    )
