"""Technical indicators + the T0 feature snapshot.

NumPy-only implementations (no pandas) to keep the dependency surface small
and the memory footprint low. All inputs are 1-D float arrays oldest-first;
indicator outputs are arrays of the same length with leading NaNs where the
window is not yet filled.

Conventions:
- Wilder smoothing is used for RSI / ATR / ADX (matches pandas defaults).
- Heavy temporaries are dropped explicitly after the final scalar is read.
- ``compute_t0`` collapses the indicator stack into a single ``T0Features``
  dataclass for one timeframe (15m / 1h / 4h).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


# ──────────────────────────────────────────────────────────────────────
# Core indicators
# ──────────────────────────────────────────────────────────────────────
def ema(values: np.ndarray, span: int) -> np.ndarray:
    """Exponential MA (uses span = (2/(N+1)) alpha, pandas-compatible)."""
    if len(values) == 0:
        return np.array([])
    alpha = 2.0 / (span + 1)
    out = np.empty_like(values, dtype=np.float64)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1 - alpha)
    return out


def sma(values: np.ndarray, window: int) -> np.ndarray:
    """Simple MA. Leading NaNs for first window-1."""
    if len(values) < window:
        return np.full(len(values), np.nan)
    out = np.full(len(values), np.nan)
    csum = np.cumsum(values, dtype=np.float64)
    out[window - 1] = csum[window - 1] / window
    out[window:] = (csum[window:] - csum[:-window]) / window
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    n = len(values)
    if n < period + 1:
        return np.full(n, np.nan)
    delta = np.diff(values, prepend=values[0])
    up = np.where(delta > 0, delta, 0.0)
    dn = np.where(delta < 0, -delta, 0.0)
    avg_up = np.full(n, np.nan)
    avg_dn = np.full(n, np.nan)
    # initial average
    avg_up[period] = up[1 : period + 1].mean()
    avg_dn[period] = dn[1 : period + 1].mean()
    for i in range(period + 1, n):
        avg_up[i] = (avg_up[i - 1] * (period - 1) + up[i]) / period
        avg_dn[i] = (avg_dn[i - 1] * (period - 1) + dn[i]) / period
    rs = avg_up / np.where(avg_dn == 0, np.nan, avg_dn)
    return 100 - 100 / (1 + rs)


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(values) < slow + signal:
        return (np.full(len(values), np.nan),) * 3
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line = ema_fast - ema_slow
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(close)
    if n < 2:
        return np.zeros(n)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's ATR."""
    n = len(close)
    tr = true_range(high, low, close)
    if n < period + 1:
        return np.full(n, np.nan)
    out = np.full(n, np.nan)
    out[period] = tr[1 : period + 1].mean()
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def adx_di(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (adx, plus_di, minus_di)."""
    n = len(close)
    if n < 2 * period:
        nan = np.full(n, np.nan)
        return nan, nan, nan
    up_move = np.diff(high, prepend=high[0])
    dn_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    tr = true_range(high, low, close)

    # Wilder smoothing
    def wilder_smooth(arr: np.ndarray, p: int) -> np.ndarray:
        out = np.full(n, np.nan)
        out[p] = arr[1 : p + 1].sum()
        for i in range(p + 1, n):
            out[i] = out[i - 1] - out[i - 1] / p + arr[i]
        return out

    tr_s = wilder_smooth(tr, period)
    plus_dm_s = wilder_smooth(plus_dm, period)
    minus_dm_s = wilder_smooth(minus_dm, period)

    plus_di = 100 * plus_dm_s / np.where(tr_s == 0, np.nan, tr_s)
    minus_di = 100 * minus_dm_s / np.where(tr_s == 0, np.nan, tr_s)
    dx = 100 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) == 0, np.nan, plus_di + minus_di)

    adx_out = np.full(n, np.nan)
    if n > 2 * period:
        adx_out[2 * period - 1] = np.nanmean(dx[period : 2 * period])
        for i in range(2 * period, n):
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period
    return adx_out, plus_di, minus_di


def bollinger(values: np.ndarray, window: int = 20, num_std: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (mid, upper, lower, width_pct of mid)."""
    n = len(values)
    if n < window:
        nan = np.full(n, np.nan)
        return nan, nan, nan, nan
    mid = sma(values, window)
    std = np.full(n, np.nan)
    # rolling std (sample, ddof=0 to match pandas .std(ddof=0))
    cumsum = np.cumsum(values, dtype=np.float64)
    cumsum2 = np.cumsum(values * values, dtype=np.float64)
    for i in range(window - 1, n):
        s1 = cumsum[i] - (cumsum[i - window] if i - window >= 0 else 0)
        s2 = cumsum2[i] - (cumsum2[i - window] if i - window >= 0 else 0)
        mean = s1 / window
        var = max(0.0, s2 / window - mean * mean)
        std[i] = math.sqrt(var)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width_pct = (upper - lower) / np.where(mid == 0, np.nan, mid) * 100
    return mid, upper, lower, width_pct


# ──────────────────────────────────────────────────────────────────────
# OHLCV container
# ──────────────────────────────────────────────────────────────────────
@dataclass
class OHLCV:
    """Lightweight OHLCV container. All arrays are aligned and oldest-first."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    ts_ms: np.ndarray  # bar open time in ms

    @classmethod
    def from_bybit_kline(cls, raw: list[list]) -> "OHLCV":
        if not raw:
            return cls(
                open=np.array([]), high=np.array([]), low=np.array([]),
                close=np.array([]), volume=np.array([]), ts_ms=np.array([], dtype=np.int64),
            )
        arr = np.asarray(raw, dtype=object)
        # Bybit kline: [startTime, open, high, low, close, volume, turnover]
        ts = np.asarray(arr[:, 0], dtype=np.int64)
        o = np.asarray(arr[:, 1], dtype=np.float64)
        h = np.asarray(arr[:, 2], dtype=np.float64)
        l = np.asarray(arr[:, 3], dtype=np.float64)
        c = np.asarray(arr[:, 4], dtype=np.float64)
        v = np.asarray(arr[:, 5], dtype=np.float64)
        return cls(open=o, high=h, low=l, close=c, volume=v, ts_ms=ts)

    def __len__(self) -> int:
        return len(self.close)


# ──────────────────────────────────────────────────────────────────────
# Aggregated T0 feature snapshot
# ──────────────────────────────────────────────────────────────────────
@dataclass
class T0Features:
    """Per-timeframe core indicator snapshot — last values, scalar floats."""

    # returns
    ret_1: float
    ret_4: float
    # EMA stack
    ema20: float
    ema50: float
    ema200: float
    ema20_50_bullish: bool
    ema_distance_pct: float  # (close - ema20) / ema20 * 100
    # oscillators
    rsi14: float
    macd_hist_norm: float  # macd_hist / close
    # trend strength
    adx14: float
    plus_di: float
    minus_di: float
    # volatility
    atr14_pct: float  # atr / close * 100
    bb_width_pct: float
    # volume
    volume_zscore: float  # over last 50 bars
    # raw close (debugging / level math)
    close: float


def _safe_last(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return float("nan")
    v = arr[-1]
    return float(v) if np.isfinite(v) else float("nan")


def _safe_get(arr: np.ndarray, idx: int) -> float:
    if len(arr) <= abs(idx):
        return float("nan")
    v = arr[idx]
    return float(v) if np.isfinite(v) else float("nan")


def compute_t0(o: OHLCV) -> T0Features:
    """Compute the T0 indicator stack for one timeframe.

    Returns a ``T0Features`` with NaN-filled fields if there are fewer than 50
    bars available (most indicators need a meaningful warm-up).
    """
    n = len(o)
    if n < 50:
        nan = float("nan")
        return T0Features(
            ret_1=nan, ret_4=nan, ema20=nan, ema50=nan, ema200=nan,
            ema20_50_bullish=False, ema_distance_pct=nan,
            rsi14=nan, macd_hist_norm=nan, adx14=nan,
            plus_di=nan, minus_di=nan, atr14_pct=nan, bb_width_pct=nan,
            volume_zscore=nan, close=_safe_last(o.close),
        )

    c = o.close
    last_close = _safe_last(c)
    prev_close = _safe_get(c, -2)
    prev4_close = _safe_get(c, -5)
    ret1 = (last_close / prev_close - 1) * 100 if math.isfinite(prev_close) and prev_close > 0 else float("nan")
    ret4 = (last_close / prev4_close - 1) * 100 if math.isfinite(prev4_close) and prev4_close > 0 else float("nan")

    e20 = ema(c, 20)
    e50 = ema(c, 50)
    e200 = ema(c, 200) if n >= 200 else e50
    ema20_last = _safe_last(e20)
    ema50_last = _safe_last(e50)
    ema200_last = _safe_last(e200)

    r14 = rsi(c, 14)
    macd_line, _, macd_h = macd(c, 12, 26, 9)
    a14 = atr(o.high, o.low, c, 14)
    adx_arr, pdi, mdi = adx_di(o.high, o.low, c, 14)
    _, _, _, bb_w = bollinger(c, 20, 2.0)

    # volume zscore over last 50 bars
    v_window = o.volume[-50:] if n >= 50 else o.volume
    v_mean = float(v_window.mean())
    v_std = float(v_window.std())
    v_last = float(o.volume[-1])
    vol_z = (v_last - v_mean) / v_std if v_std > 0 else 0.0

    macd_h_last = _safe_last(macd_h)
    atr_last = _safe_last(a14)

    feat = T0Features(
        ret_1=round(ret1, 4),
        ret_4=round(ret4, 4),
        ema20=round(ema20_last, 6),
        ema50=round(ema50_last, 6),
        ema200=round(ema200_last, 6),
        ema20_50_bullish=ema20_last > ema50_last if math.isfinite(ema20_last) and math.isfinite(ema50_last) else False,
        ema_distance_pct=round((last_close - ema20_last) / ema20_last * 100, 4) if math.isfinite(ema20_last) and ema20_last > 0 else float("nan"),
        rsi14=round(_safe_last(r14), 2),
        macd_hist_norm=round(macd_h_last / last_close, 6) if math.isfinite(macd_h_last) and last_close > 0 else float("nan"),
        adx14=round(_safe_last(adx_arr), 2),
        plus_di=round(_safe_last(pdi), 2),
        minus_di=round(_safe_last(mdi), 2),
        atr14_pct=round(atr_last / last_close * 100, 4) if math.isfinite(atr_last) and last_close > 0 else float("nan"),
        bb_width_pct=round(_safe_last(bb_w), 4),
        volume_zscore=round(vol_z, 3),
        close=round(last_close, 6),
    )
    return feat
