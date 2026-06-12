"""Technical indicators (numpy) — EMA, Wilder ATR/ADX, Donchian channel.

These are the exact formulas validated in the backtest research, kept here as
the single source so the live signal and the backtest compute identically.
Inputs are 1-D float arrays ordered oldest→newest.
"""
from __future__ import annotations

import numpy as np


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ru = _wilder(up, n); rd = _wilder(dn, n)
    rs = ru / np.maximum(rd, 1e-9)
    return 100 - 100 / (1 + rs)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    a = 1 / n
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = np.empty_like(c, dtype=float)
    pc[0] = c[0]; pc[1:] = c[:-1]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    return _wilder(true_range(h, l, c), n)


def adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    plus_dm = np.concatenate([[0.0], np.where((up > dn) & (up > 0), up, 0.0)])
    minus_dm = np.concatenate([[0.0], np.where((dn > up) & (dn > 0), dn, 0.0)])
    atr_n = _wilder(true_range(h, l, c), n)
    pdi = 100 * _wilder(plus_dm, n) / np.maximum(atr_n, 1e-9)
    mdi = 100 * _wilder(minus_dm, n) / np.maximum(atr_n, 1e-9)
    dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    return _wilder(dx, n)


def donchian_prev(h: np.ndarray, l: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Prior-N-bar high and low EXCLUDING the current bar (the breakout level
    a bar must close beyond). Index i looks back over [i-n, i)."""
    hi = np.full_like(h, np.nan, dtype=float)
    lo = np.full_like(l, np.nan, dtype=float)
    for i in range(n, len(h)):
        hi[i] = h[i - n:i].max()
        lo[i] = l[i - n:i].min()
    return hi, lo
