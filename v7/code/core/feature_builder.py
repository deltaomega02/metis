"""Builds the compact indicator snapshot fed to the AI for one symbol.

Fetches 1H (primary) + 4H + 1D klines and reduces each to a small set of
trend/momentum/volatility readings (EMA stack, RSI, ADX, ATR%, last close) plus
the 1H Donchian breakout levels. Kept small on purpose — short prompts keep the
reasoning model on-task and cheap. Raw kline arrays are dropped after reduction.
"""
from __future__ import annotations

import numpy as np

from config.settings import ANALYSIS
from core.indicators import adx, atr, donchian_prev, ema, rsi
from exchange.bybit_client import get_bybit_client


def _reduce(h, l, c) -> dict:
    last = float(c[-1])
    e20 = float(ema(c, 20)[-1]); e50 = float(ema(c, 50)[-1])
    e200 = float(ema(c, 200)[-1]) if len(c) >= 200 else None
    ax = float(adx(h, l, c, ANALYSIS.ADX_LEN)[-1])
    av = float(atr(h, l, c, ANALYSIS.ATR_LEN)[-1])
    rs = float(rsi(c, ANALYSIS.RSI_LEN)[-1])
    return {
        "close": round(last, 6),
        "ema20": round(e20, 6), "ema50": round(e50, 6),
        "ema200": round(e200, 6) if e200 else None,
        "trend": "up" if e20 > e50 else "down",
        "price_vs_ema50_pct": round((last - e50) / e50 * 100, 2) if e50 else 0,
        "rsi": round(rs, 1), "adx": round(ax, 1),
        "trending": ax > 22, "atr_pct": round(av / last * 100, 2) if last else 0,
    }


class FeatureBuilder:
    def __init__(self):
        self.bybit = get_bybit_client()

    async def build(self, symbol: str) -> dict | None:
        out = {"symbol": symbol}
        # primary 1H — also drives price + breakout levels
        kp = await self._closed(symbol, ANALYSIS.PRIMARY_INTERVAL)
        if kp is None:
            return None
        h, l, c = kp
        out["tf_1h"] = _reduce(h, l, c)
        out["price"] = round(float(c[-1]), 6)
        dhi, dlo = donchian_prev(h, l, ANALYSIS.DONCHIAN_N)
        cur = float(c[-1])
        out["breakout"] = {
            "donchian_high": round(float(dhi[-1]), 6),
            "donchian_low": round(float(dlo[-1]), 6),
            "dist_to_high_pct": round((float(dhi[-1]) - cur) / cur * 100, 2),
            "dist_to_low_pct": round((cur - float(dlo[-1])) / cur * 100, 2),
        }
        # context timeframes
        for tf, label in zip(ANALYSIS.CONTEXT_INTERVALS, ("tf_4h", "tf_1d")):
            k = await self._closed(symbol, tf)
            if k is not None:
                out[label] = _reduce(*k)
        return out

    async def _closed(self, symbol: str, interval: str):
        rows = await self.bybit.get_kline(symbol, interval, ANALYSIS.KLINE_LOOKBACK)
        rows = sorted(rows, key=lambda r: int(r[0]))[:-1]  # drop forming bar
        need = max(ANALYSIS.EMA_PERIODS[0], ANALYSIS.ADX_LEN, ANALYSIS.DONCHIAN_N) + 5
        if len(rows) < need:
            return None
        h = np.array([float(r[2]) for r in rows])
        l = np.array([float(r[3]) for r in rows])
        c = np.array([float(r[4]) for r in rows])
        return h, l, c
