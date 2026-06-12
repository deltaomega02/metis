"""Derivatives flow features.

Builds the T1 block from raw Bybit endpoints:

- ``funding_*``: current + 24h avg + 7d z-score from the 8h funding snapshots.
- ``oi_*``: 1h / 4h percentage change and z-score from 5-minute open interest.
- ``liq_*``: 24h long/short liquidation imbalance (when the stream worker has
  liquidation data; otherwise zeros).
- ``taker_buy_sell_ratio_*``: 1h / 4h taker buy share computed from the public
  trade buffer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class T1Derivatives:
    """Per-symbol derivatives flow features for one cycle."""

    funding_current_8h: float          # decimal (e.g. 0.0001 = 0.01%)
    funding_avg_24h: float
    funding_zscore_7d: float           # over last ~21 snapshots (8h × 21 = 7d)
    oi_change_1h_pct: float
    oi_change_4h_pct: float
    oi_zscore_7d: float
    liq_long_24h_usd: float
    liq_short_24h_usd: float
    liq_imbalance_24h: float           # (long - short) / (long + short), -1..+1
    taker_buy_sell_ratio_1h: float     # buy/(buy+sell), 0..1
    taker_buy_sell_ratio_4h: float


def _safe(x: Optional[float]) -> float:
    if x is None:
        return float("nan")
    try:
        f = float(x)
        return f if math.isfinite(f) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _zscore(values: list[float], current: float) -> float:
    if not values:
        return float("nan")
    arr = np.asarray([v for v in values if math.isfinite(v)], dtype=np.float64)
    if len(arr) < 5:
        return float("nan")
    mean = float(arr.mean())
    std = float(arr.std())
    if std <= 0:
        return 0.0
    return (current - mean) / std


def compute_derivatives(
    funding_history: list[dict],  # Bybit funding history: newest first
    oi_history: list[dict],       # Bybit open-interest 5min, newest first
    recent_trades: list[dict],    # public trade buffer (1h+ window)
    liq_long_24h_usd: float = 0.0,
    liq_short_24h_usd: float = 0.0,
) -> T1Derivatives:
    # funding
    funding_rates = [_safe(item.get("fundingRate")) for item in funding_history]
    funding_rates = [f for f in funding_rates if math.isfinite(f)]
    funding_current = funding_rates[0] if funding_rates else float("nan")
    funding_24h = funding_rates[:3]  # 3 × 8h
    funding_avg_24h = float(np.mean(funding_24h)) if funding_24h else float("nan")
    funding_7d_window = funding_rates[:21]  # 21 × 8h ≈ 7d
    funding_z = _zscore(funding_7d_window[1:], funding_current) if funding_7d_window else float("nan")

    # open interest — Bybit returns "openInterest" (contract count, or symbol-specific)
    oi_vals = [_safe(item.get("openInterest")) for item in oi_history]
    oi_vals = [v for v in oi_vals if math.isfinite(v)]
    oi_current = oi_vals[0] if oi_vals else float("nan")
    # 1h ago = 12th sample (5min × 12 = 60min), 4h ago = 48th
    oi_1h = oi_vals[12] if len(oi_vals) > 12 else float("nan")
    oi_4h = oi_vals[48] if len(oi_vals) > 48 else float("nan")
    oi_chg_1h = (oi_current / oi_1h - 1) * 100 if math.isfinite(oi_current) and math.isfinite(oi_1h) and oi_1h > 0 else float("nan")
    oi_chg_4h = (oi_current / oi_4h - 1) * 100 if math.isfinite(oi_current) and math.isfinite(oi_4h) and oi_4h > 0 else float("nan")
    # 7d ≈ 2016 samples (5min × 12 × 24 × 7). We won't usually have that many here;
    # fall back to whatever window we have.
    oi_window_7d = oi_vals[1:]
    oi_z = _zscore(oi_window_7d, oi_current)

    # Taker buy/sell ratio — Bybit recent-trade returns ~1000 trades, often a
    # fraction of an hour at busy times. For the full hourly aggregate the
    # stream worker is authoritative; here we compute on the available window.
    buy_vol_1h = 0.0
    sell_vol_1h = 0.0
    buy_vol_4h = 0.0
    sell_vol_4h = 0.0
    now_ms = 0
    if recent_trades:
        try:
            now_ms = max(int(t.get("time", 0)) for t in recent_trades if t.get("time"))
        except (TypeError, ValueError):
            now_ms = 0
    one_hour_ms = 3600_000
    four_hour_ms = 4 * one_hour_ms
    for t in recent_trades:
        try:
            ts = int(t.get("time", 0))
            size = float(t.get("size", 0))
            side = (t.get("side") or "").lower()
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        in_1h = (now_ms - ts) <= one_hour_ms
        in_4h = (now_ms - ts) <= four_hour_ms
        if side == "buy":
            if in_1h: buy_vol_1h += size
            if in_4h: buy_vol_4h += size
        elif side == "sell":
            if in_1h: sell_vol_1h += size
            if in_4h: sell_vol_4h += size
    ratio_1h = buy_vol_1h / (buy_vol_1h + sell_vol_1h) if (buy_vol_1h + sell_vol_1h) > 0 else float("nan")
    ratio_4h = buy_vol_4h / (buy_vol_4h + sell_vol_4h) if (buy_vol_4h + sell_vol_4h) > 0 else float("nan")

    # liquidation
    liq_total = liq_long_24h_usd + liq_short_24h_usd
    liq_imb = (liq_long_24h_usd - liq_short_24h_usd) / liq_total if liq_total > 0 else 0.0

    return T1Derivatives(
        funding_current_8h=round(funding_current, 8) if math.isfinite(funding_current) else float("nan"),
        funding_avg_24h=round(funding_avg_24h, 8) if math.isfinite(funding_avg_24h) else float("nan"),
        funding_zscore_7d=round(funding_z, 3) if math.isfinite(funding_z) else float("nan"),
        oi_change_1h_pct=round(oi_chg_1h, 3) if math.isfinite(oi_chg_1h) else float("nan"),
        oi_change_4h_pct=round(oi_chg_4h, 3) if math.isfinite(oi_chg_4h) else float("nan"),
        oi_zscore_7d=round(oi_z, 3) if math.isfinite(oi_z) else float("nan"),
        liq_long_24h_usd=round(liq_long_24h_usd, 2),
        liq_short_24h_usd=round(liq_short_24h_usd, 2),
        liq_imbalance_24h=round(liq_imb, 3),
        taker_buy_sell_ratio_1h=round(ratio_1h, 3) if math.isfinite(ratio_1h) else float("nan"),
        taker_buy_sell_ratio_4h=round(ratio_4h, 3) if math.isfinite(ratio_4h) else float("nan"),
    )
