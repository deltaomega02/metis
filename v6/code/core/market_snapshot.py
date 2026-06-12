"""Shared market snapshot — the engine computes every coin's indicators once per
cycle and writes them here; the dashboard reads this file instead of calling Bybit.

This makes the engine the single source of exchange data: the dashboard does zero
Bybit calls, so it can never contend for the per-IP rate limit (retCode 10006).
The snapshot refreshes every 4h cycle — the dashboard is intentionally only as
fresh as the last closed bar, which is all the 4h strategy needs.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from config.settings import STRATEGY

# numpy/indicators are imported lazily inside compute_* so that the dashboard, which
# only reads the snapshot, never pulls numpy into its process (keeps it lightweight).


def compute_coin(symbol: str, klines: list[list]) -> dict | None:
    """Indicator summary for one symbol on the last closed bar — identical math to
    the breakout signal. klines exclude the still-forming bar. None if too short."""
    import numpy as np
    from core.indicators import adx, atr, donchian_prev, ema

    rows = sorted(klines, key=lambda r: int(r[0]))
    if len(rows) < STRATEGY.EMA_SLOW + 5:
        return None
    h = np.array([float(r[2]) for r in rows]); l = np.array([float(r[3]) for r in rows])
    c = np.array([float(r[4]) for r in rows])
    ef = ema(c, STRATEGY.EMA_FAST); es = ema(c, STRATEGY.EMA_SLOW)
    ax = adx(h, l, c, STRATEGY.ADX_LEN); av = atr(h, l, c, STRATEGY.ATR_LEN)
    dhi, dlo = donchian_prev(h, l, STRATEGY.DONCHIAN_N)
    i = len(c) - 1
    price = float(c[i]); adxv = float(ax[i]); brk = float(dhi[i]); brk_lo = float(dlo[i])
    trending = bool(adxv > STRATEGY.ADX_MIN); up = bool(ef[i] > es[i])
    return {"symbol": symbol, "price": price, "ema20": float(ef[i]), "ema50": float(es[i]),
            "adx": adxv, "trending": trending, "up": up,
            "atrp": float(av[i]) / price * 100 if price else 0.0,
            # 롱 돌파(신고가): dist_hi>0 = 가격이 돌파선 아래(올라야 진입)
            "brk": brk, "dist_hi": (brk - price) / price * 100 if price else 0.0,
            "long_cand": bool(trending and up and price > brk),
            # 숏 돌파(신저가): dist_lo>0 = 가격이 돌파선 위(내려야 진입)
            "brk_lo": brk_lo, "dist_lo": (price - brk_lo) / price * 100 if price else 0.0,
            "short_cand": bool(trending and not up and price < brk_lo)}


def compute_gates(btc_klines: list[list]) -> dict:
    from core.breakout_signal import gate_open
    return {k: bool(gate_open(btc_klines, k)) for k in ("none", "ema20_50", "ema50_200")}


def write_snapshot(path, cycle: str, gates: dict, coins: dict):
    """Atomic write (temp + os.replace) so the dashboard never reads a half-written
    file even though they are separate processes."""
    payload = {"updated_utc": datetime.now(timezone.utc).isoformat(),
               "cycle": cycle, "gates": gates, "coins": coins}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(str(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_snapshot(path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None
