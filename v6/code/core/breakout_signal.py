"""Breakout entry signal — the deterministic core that carries the edge.

Evaluates one symbol's closed klines and returns an entry intent (or None).
Rule (validated in research/risk_sweep.py — LONG-ONLY + BTC uptrend gate):
  LONG : last close > prior Donchian-N high  AND  EMA_fast > EMA_slow  AND  ADX > min
Shorts are NOT taken: every realtime regime test left them net-negative, so the
strategy is long-only and is additionally gated to BTC uptrend (see btc_uptrend),
applied by the engine before any entry. Structural stop = entry − ATR_K · ATR.
There is no fixed target; the position exits when the close crosses back through
the slow EMA (see should_exit).

The signal is read on the LAST CLOSED bar only — the caller passes klines that
exclude the still-forming bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from config.settings import STRATEGY
from core.indicators import adx, atr, donchian_prev, ema


@dataclass
class EntryIntent:
    symbol: str
    side: str            # "Buy" | "Sell"
    reference_price: float
    stop_price: float
    stop_distance: float  # absolute price distance entry→stop (for sizing)
    take_profit: float    # SHORT-only fixed R-target (0.0 for longs → trend exit)
    adx: float
    atr: float


def _arrays(klines: list[list]) -> tuple:
    """klines: Bybit list rows [ts, o, h, l, c, v, turnover], any order →
    returns ascending o,h,l,c arrays."""
    rows = sorted(klines, key=lambda r: int(r[0]))
    o = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    l = np.array([float(r[3]) for r in rows])
    c = np.array([float(r[4]) for r in rows])
    return o, h, l, c


def evaluate(symbol: str, klines: list[list], allow_short: bool = False) -> Optional[EntryIntent]:
    """Return an EntryIntent if the last closed bar triggers a breakout, else None.
    Long breakout always; short breakout only when allow_short (the both-ways arm)."""
    need = max(STRATEGY.EMA_SLOW, STRATEGY.DONCHIAN_N, STRATEGY.ADX_LEN) + 5
    if len(klines) < need:
        return None
    o, h, l, c = _arrays(klines)
    ef = ema(c, STRATEGY.EMA_FAST)
    es = ema(c, STRATEGY.EMA_SLOW)
    ax = adx(h, l, c, STRATEGY.ADX_LEN)
    av = atr(h, l, c, STRATEGY.ATR_LEN)
    dhi, dlo = donchian_prev(h, l, STRATEGY.DONCHIAN_N)

    i = len(c) - 1
    if not np.isfinite(av[i]) or av[i] <= 0 or ax[i] <= STRATEGY.ADX_MIN:
        return None

    risk = STRATEGY.ATR_K * av[i]
    if c[i] > dhi[i] and ef[i] > es[i]:
        entry = c[i]
        # long: no fixed target — ride until the trend ends (should_exit)
        return EntryIntent(symbol, "Buy", entry, entry - risk, risk, 0.0, float(ax[i]), float(av[i]))
    if allow_short and c[i] < dlo[i] and ef[i] < es[i]:
        entry = c[i]
        # short: fixed R-target (trend exit gives the gain back in a downtrend)
        tp = entry - STRATEGY.SHORT_TP_R * risk
        return EntryIntent(symbol, "Sell", entry, entry + risk, risk, tp, float(ax[i]), float(av[i]))
    return None


def gate_open(klines: list[list], kind: str) -> bool:
    """BTC regime gate by kind — the global long-entry permission for an arm:
      none       : always open (no regime filter — most aggressive)
      ema20_50   : ema20 > ema50 (fast; catches turns early, thinner edge)
      ema50_200  : ema50 > ema200 (slow golden cross; robust per-trade edge, late)
    """
    if kind == "none":
        return True
    if not klines:
        return False
    _, _, _, c = _arrays(klines)
    i = len(c) - 1
    if kind == "ema20_50":
        if len(klines) < STRATEGY.EMA_SLOW + 5:
            return False
        return bool(ema(c, STRATEGY.EMA_FAST)[i] > ema(c, STRATEGY.EMA_SLOW)[i])
    if kind == "ema50_200":
        if len(klines) < STRATEGY.GATE_EMA_LONG + 5:
            return False
        return bool(ema(c, STRATEGY.EMA_SLOW)[i] > ema(c, STRATEGY.GATE_EMA_LONG)[i])
    return False


def btc_uptrend(klines: list[list]) -> bool:
    """Back-compat: the single-arm live gate (ema20 > ema50)."""
    return gate_open(klines, "ema20_50")


def should_exit(side: str, klines: list[list]) -> bool:
    """Trend-end exit for LONGS only: True when the last closed bar closes back below
    the slow EMA. Shorts do NOT use this — a short's EMA50 re-cross only triggers after
    price has rebounded past entry, giving the whole gain back (research/exit_redesign.py);
    shorts exit on their fixed R-target instead. So this returns False for shorts."""
    if side != "Buy" or len(klines) < STRATEGY.EMA_SLOW + 2:
        return False
    _, _, _, c = _arrays(klines)
    es = ema(c, STRATEGY.EMA_SLOW)
    i = len(c) - 1
    return c[i] < es[i]
