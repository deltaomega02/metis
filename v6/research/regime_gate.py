"""Does a simple 'trade only when BTC is in a clean uptrend' gate beat trading
always? Tests a regime FILTER (avoid bad markets) vs regime-specific strategies.

BTC trend is judged in real time (EMA50>EMA200 on BTC 4h, using only bars up to
each trade's entry time — no look-ahead). Compares, IS vs OOS:
  all            : every breakout trade (no gate)
  btc_up         : only when BTC uptrend (long+short)
  btc_up_long    : only BTC-uptrend AND long  (the hypothesised sweet spot)
  btc_down_short : only BTC-downtrend AND short (does the 'bear→short' idea work?)

"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import ema
from edge_scan import precompute, signal_dir
from regime_analysis import bt_side

DATA = Path(__file__).resolve().parent / "data"


def main():
    b = np.load(DATA / "BTCUSDT_4h.npz")
    bt_t = b["t"].astype(np.int64)
    bc = b["c"]
    btc_up = ema(bc, 50) > ema(bc, 200)   # bool per BTC bar

    def up_at(ts: int) -> bool:
        idx = int(np.searchsorted(bt_t, ts, side="right")) - 1
        return idx >= 0 and bool(btc_up[idx])

    alltr = []
    for f in sorted(DATA.glob("*_4h.npz")):
        d = np.load(f)
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        alltr += bt_side(o, h, l, c, t, ind, dirs)   # (t_in, R, side)

    all_t = sorted(x[0] for x in alltr)
    split = all_t[int(len(all_t) * 0.70)]

    gates = {
        "all": lambda ti, s: True,
        "btc_up (롱+숏)": lambda ti, s: up_at(ti),
        "btc_up_long": lambda ti, s: up_at(ti) and s == "L",
        "btc_down_short": lambda ti, s: (not up_at(ti)) and s == "S",
    }

    def stats(filt, phase):
        R = np.array([r for ti, r, s in alltr
                      if (ti < split) == (phase == "IS") and filt(ti, s)])
        if len(R) < 10:
            return None
        gp = R[R > 0].sum(); gl = -R[R < 0].sum()
        return len(R), float(R.mean()), float(gp / max(gl, 1e-9))

    print(f"total trades={len(alltr)}  BTC-trend gated (realtime EMA50>200)\n")
    print(f"{'gate':>16} | {'IS n':>5} {'IS expR':>8} {'IS PF':>6} | {'OOS n':>6} {'OOS expR':>8} {'OOS PF':>6}")
    for name, filt in gates.items():
        a = stats(filt, "IS"); b2 = stats(filt, "OOS")
        ai = f"{a[0]:>5} {a[1]:>+8.3f} {a[2]:>6.2f}" if a else f"{'-':>5} {'-':>8} {'-':>6}"
        bi = f"{b2[0]:>6} {b2[1]:>+8.3f} {b2[2]:>6.2f}" if b2 else f"{'-':>6} {'-':>8} {'-':>6}"
        print(f"{name:>16} | {ai} | {bi}")


if __name__ == "__main__":
    main()
