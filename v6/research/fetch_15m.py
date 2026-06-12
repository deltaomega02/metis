"""Fetch 15m Bybit USDT-perp klines (SOL/ETH/BTC) and cache as .npz.

Pages backward from now via /v5/market/kline (newest-first, max 1000/call),
accumulates ~`months` of history, sorts ascending, and saves arrays
t(ms), o, h, l, c, v.  BTC is fetched too — used only as a regime/context
filter in the backtest, never traded.

"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import requests

BASE = "https://api.bybit.com/v5/market/kline"
DATA_DIR = Path(__file__).resolve().parent / "data"


def fetch_all(symbol: str, interval: str = "15", months: int = 18) -> dict:
    target = int(months * 30 * 24 * (60 / int(interval)))  # approx bar count
    end = int(time.time() * 1000)
    rows: list[list[str]] = []
    seen_oldest = None
    while len(rows) < target:
        r = requests.get(
            BASE,
            params={"category": "linear", "symbol": symbol,
                    "interval": interval, "limit": 1000, "end": end},
            timeout=15,
        ).json()
        lst = (r.get("result") or {}).get("list") or []
        if not lst:
            break
        rows.extend(lst)
        oldest = int(lst[-1][0])
        if seen_oldest is not None and oldest >= seen_oldest:
            break  # no progress → exchange history exhausted
        seen_oldest = oldest
        end = oldest - 1
        time.sleep(0.12)
    # dedup by ts, sort ascending
    by_ts = {int(x[0]): x for x in rows}
    ordered = [by_ts[k] for k in sorted(by_ts)]
    t = np.array([int(x[0]) for x in ordered], dtype=np.int64)
    o = np.array([float(x[1]) for x in ordered])
    h = np.array([float(x[2]) for x in ordered])
    l = np.array([float(x[3]) for x in ordered])
    c = np.array([float(x[4]) for x in ordered])
    v = np.array([float(x[5]) for x in ordered])
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


_LABEL = {"15": "15m", "60": "1h", "240": "4h", "D": "1d"}


def main():
    interval = sys.argv[1] if len(sys.argv) > 1 else "15"
    months = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    label = _LABEL.get(interval, interval)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    syms = (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT",
        "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT",
        "ATOMUSDT", "FILUSDT", "UNIUSDT", "AAVEUSDT", "TONUSDT", "MATICUSDT",
    )
    # allow overriding the symbol list via argv[3] as comma-separated (optional)
    if len(sys.argv) > 3:
        syms = tuple(sys.argv[3].split(","))
    for sym in syms:
        d = fetch_all(sym, interval, months)
        out = DATA_DIR / f"{sym}_{label}.npz"
        np.savez_compressed(out, **d)
        n = len(d["t"])
        if n:
            from datetime import datetime, timezone
            t0 = datetime.fromtimestamp(d["t"][0] / 1000, tz=timezone.utc)
            t1 = datetime.fromtimestamp(d["t"][-1] / 1000, tz=timezone.utc)
            span_days = (d["t"][-1] - d["t"][0]) / 1000 / 86400
            print(f"{sym}: {n} bars  {t0:%Y-%m-%d} → {t1:%Y-%m-%d}  ({span_days:.0f}d)  → {out.name}")
        else:
            print(f"{sym}: NO DATA")


if __name__ == "__main__":
    main()
