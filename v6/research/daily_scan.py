"""Daily-horizon scan — the timeframe we never tested deeply (only 15m/1h/4h).
Resamples 4h→daily (6 bars), runs all 4 strategies, reports IS/OOS expR with
long/short split across 23 coins. Literature says momentum is most robust at
daily/weekly; this checks if daily beats the fragile intraday edge.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from regime_strategy_matrix import bt

DATA = Path(__file__).resolve().parent / "data"
STRATS = ["TREND", "BREAKOUT", "MEANREV", "VOLBREAK"]
EXIT = {"TREND": "trend", "BREAKOUT": "trend", "VOLBREAK": "trend", "MEANREV": "fixed"}


def resample_d(o, h, l, c, t, f=6):
    n = (len(c) // f) * f
    return (o[:n].reshape(-1, f)[:, 0], h[:n].reshape(-1, f).max(1),
            l[:n].reshape(-1, f).min(1), c[:n].reshape(-1, f)[:, -1],
            t[:n].reshape(-1, f)[:, 0])


def main():
    coins = [f.name[:-7] for f in DATA.glob("*_4h.npz")]
    data = {s: [] for s in STRATS}
    for coin in coins:
        d = np.load(DATA / f"{coin}_4h.npz")
        o, h, l, c, t = resample_d(d["o"], d["h"], d["l"], d["c"], d["t"])
        if len(c) < 120:
            continue
        ind = precompute(o, h, l, c)
        for s in STRATS:
            dirs = signal_dir(s, o, h, l, c, ind)
            data[s] += bt(o, h, l, c, t, ind, dirs, exit_mode=EXIT[s])

    all_t = sorted(x[0] for s in STRATS for x in data[s])
    split = all_t[int(len(all_t) * 0.70)]

    def pf(a): g = a[a > 0].sum(); return g / max(-a[a < 0].sum(), 1e-9)

    print("DAILY horizon (4h×6) · 23코인 · 전략별 IS/OOS\n")
    print(f"{'strat':>9} | {'n':>4} {'IS expR':>8} {'OOS expR':>8} {'OOS PF':>6} | 롱(OOS) / 숏(OOS)")
    for s in STRATS:
        tr = data[s]
        if len(tr) < 30:
            print(f"{s:>9} | n={len(tr)} 적음"); continue
        osR = np.array([r for ti, r, sd in tr if ti >= split])
        isR = np.array([r for ti, r, sd in tr if ti < split])
        Lo = np.array([r for ti, r, sd in tr if ti >= split and sd == "L"])
        So = np.array([r for ti, r, sd in tr if ti >= split and sd == "S"])
        ls = f"롱 {Lo.mean():+.3f}({len(Lo)})" if len(Lo) >= 10 else "롱 -"
        ss = f"숏 {So.mean():+.3f}({len(So)})" if len(So) >= 10 else "숏 -"
        print(f"{s:>9} | {len(tr):>4} {isR.mean():>+8.3f} {osR.mean():>+8.3f} {pf(osR):>6.2f} | {ls} / {ss}")


if __name__ == "__main__":
    main()
