"""Is the live short edge luck? — split downtrend shorts by period.

Live ls shorts are winning ~80% in a near-straight-line drop. Backtest downtrend
shorts win only 28% on average (519 trades) → net negative, because most downtrends
have frequent bear rallies that stop shorts out. If the win-rate swings wildly by
period (some downtrend windows 50%+, others 15%), then the live run is just sitting
in a lucky straight-line window, not a durable edge. This buckets downtrend shorts
by entry half-year and shows n / win% / sumR, so we see the spread.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from edge_scan import precompute, signal_dir, COINS
from exit_redesign import trades_coin, DATA
from regime_ls import btc_regime_series, regime_at


def half(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year}-H{1 if dt.month <= 6 else 2}"


def main():
    ts, reg = btc_regime_series()
    buck = defaultdict(list)   # half-year -> [R] for downtrend shorts
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tl = [int(x) for x in t]
        for (ti, to, R) in trades_coin(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                                       exit_mode="mixed", trail_k=0.0, slip_bps=2,
                                       fund_rate=0.0001, side_filter=0):
            idx = bisect.bisect_left(tl, ti)
            if idx < len(dirs) and dirs[idx] < 0 and regime_at(ts, reg, ti) == "down":
                buck[half(ti)].append(R)

    print("=== 하락장 숏: 반기별 (win율 출렁이면 = 국면 의존 = 라이브는 운 좋은 창) ===")
    print(f"   {'반기':>8} | {'n':>4} {'win':>5} {'sumR':>8} {'expR':>7}")
    wins_by = []
    for hy in sorted(buck):
        Rs = np.array(buck[hy])
        if len(Rs) < 5:
            continue
        w = (Rs > 0).mean()
        wins_by.append(w)
        bar = "+" * int(max(Rs.sum(), 0) / 5) + "-" * int(max(-Rs.sum(), 0) / 5)
        print(f"   {hy:>8} | {len(Rs):>4} {w*100:>4.0f}% {Rs.sum():>+8.1f} {Rs.mean():>+7.3f}  {bar}")

    allR = np.array([r for v in buck.values() for r in v])
    print(f"\n   전체 하락장 숏: n={len(allR)} win={ (allR>0).mean()*100:.0f}% "
          f"expR={allR.mean():+.3f} sumR={allR.sum():+.1f}")
    print(f"   반기별 win율 범위: {min(wins_by)*100:.0f}% ~ {max(wins_by)*100:.0f}% "
          f"(라이브 현재 ~80%)")
    pos = sum(1 for v in buck.values() if len(v) >= 5 and np.sum(v) > 0)
    tot = sum(1 for v in buck.values() if len(v) >= 5)
    print(f"   숏이 net+ 인 반기: {pos}/{tot}  → 하락장 숏은 '가끔만' 번다")


if __name__ == "__main__":
    main()
