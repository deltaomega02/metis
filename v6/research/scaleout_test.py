"""scale-out vs mixed (short side) — does locking half early beat riding all to 2.5R?

mixed: a short closes ALL at fixed 2.5R (or SL). A bear rally can take back the whole
unrealized before the 2.5R target fires. scale-out: close HALF at a near target
(r1≈1~1.5R), ride the other HALF to a far target (r2≈2.5~3R) — caps the give-back at
the cost of trimming the big runners. Longs keep the trend exit either way. Compares
overall (IS/OOS, portfolio) + downtrend short net, friction-adjusted.

"""
from __future__ import annotations

import bisect
from collections import defaultdict

import numpy as np

from edge_scan import precompute, signal_dir, COINS, EMA_S, DON_N, BB_N, VB_N
from exit_redesign import DATA
from validate_breakout import portfolio_sim
from backtest_trend_15m import FEE_RT
from regime_ls import btc_regime_series, regime_at


def trades(o, h, l, c, t, ind, dirs, *, mode, r1=1.0, r2=2.5, R_full=2.5, atr_k=1.5,
           slip_bps=2, fund=0.0001):
    """mode='mixed' (short all@R_full) or 'scaleout' (short half@r1 + half@r2).
    Long = trend exit in both. Returns (t_in, t_out, R_net)."""
    cost = FEE_RT + 2 * slip_bps / 10000.0
    es, av = ind["es"], ind["av"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    out = []; i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; rk = atr_k * av[i]
        if rk <= 0 or not np.isfinite(rk):
            i += 1; continue
        cost_R = cost / (rk / entry)              # round-trip cost in R units
        if di == 1:                               # long: trend exit (unchanged)
            sl = entry - rk; j = i + 1; expx = None
            while j < n:
                if l[j] <= sl: expx = sl; break
                if c[j] < es[j]: expx = c[j]; break
                j += 1
            if expx is None: break
            gr = (expx - entry) / entry / (rk / entry)
            out.append((int(t[i]), int(t[j]), gr - cost_R - (j - i) * 0.5 * fund / (rk / entry)))
            i = j + 1; continue
        # short
        sl = entry + rk
        if mode == "mixed":
            tp = entry - R_full * rk; j = i + 1; expx = None
            while j < n:
                if h[j] >= sl: expx = sl; break
                if l[j] <= tp: expx = tp; break
                j += 1
            if expx is None: break
            gr = -(expx - entry) / entry / (rk / entry)
            out.append((int(t[i]), int(t[j]), gr - cost_R - (j - i) * 0.5 * fund / (rk / entry)))
            i = j + 1; continue
        # scaleout: half @ r1, half @ r2, shared SL
        tp1 = entry - r1 * rk; tp2 = entry - r2 * rk
        f1 = f2 = False; rsum = 0.0; j = i + 1; jend = None
        while j < n:
            if h[j] >= sl:
                if not f1: rsum += 0.5 * (-1.0)
                if not f2: rsum += 0.5 * (-1.0)
                jend = j; break
            if not f1 and l[j] <= tp1: rsum += 0.5 * r1; f1 = True
            if not f2 and l[j] <= tp2: rsum += 0.5 * r2; f2 = True
            if f1 and f2: jend = j; break
            j += 1
        if jend is None: break
        # friction: entry once + two partial closes ≈ 1.25× a round-trip cost
        net = rsum - cost_R * 1.25 - (jend - i) * 0.5 * fund / (rk / entry)
        out.append((int(t[i]), int(t[jend]), net))
        i = jend + 1
    return out


def collect(mode, **kw):
    out = []
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists(): continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out.append((coin, o, h, l, c, t, ind, dirs, trades(o, h, l, c, t, ind, dirs, mode=mode, **kw)))
    return out


def downtrend_short(coll, ts, reg):
    """downtrend short trades only → sumR, win."""
    Rs = []
    for (coin, o, h, l, c, t, ind, dirs, trs) in coll:
        tl = [int(x) for x in t]
        for (ti, to, R) in trs:
            idx = bisect.bisect_left(tl, ti)
            if idx < len(dirs) and dirs[idx] < 0 and regime_at(ts, reg, ti) == "down":
                Rs.append(R)
    Rs = np.array(Rs)
    return len(Rs), (Rs > 0).mean() * 100 if len(Rs) else 0, Rs.sum()


def is_oos(coll):
    allt = sorted([x for (_, *_, trs) in coll for x in trs])
    s = int(len(allt) * 0.70)
    ri = np.array([x[2] for x in allt[:s]]); ro = np.array([x[2] for x in allt[s:]])
    return ri.mean(), ro.mean(), len(allt)


def main():
    ts, reg = btc_regime_series()
    print("=== scale-out vs mixed (숏만 변경, 롱=trend 동일) · 8코인 4h 마찰후 ===\n")
    print(f"{'구성':18} | {'n':>4} {'IS':>7} {'OOS':>7} {'PF':>5} {'total':>8} {'MDD':>6} | {'하락장숏':>8} {'win':>4}")
    configs = [
        ("mixed(현재) 전량2.5R", dict(mode="mixed", R_full=2.5)),
        ("scaleout 1.0/2.5",     dict(mode="scaleout", r1=1.0, r2=2.5)),
        ("scaleout 1.5/3.0",     dict(mode="scaleout", r1=1.5, r2=3.0)),
        ("scaleout 1.0/3.0",     dict(mode="scaleout", r1=1.0, r2=3.0)),
        ("scaleout 1.5/2.5",     dict(mode="scaleout", r1=1.5, r2=2.5)),
    ]
    for name, kw in configs:
        coll = collect(**kw)
        allt = sorted([x for (_, *_, trs) in coll for x in trs])
        ise, oose, nn = is_oos(coll)
        pm = portfolio_sim(allt, risk_frac=0.0075, cap=4)
        dn, dwin, dsum = downtrend_short(coll, ts, reg)
        print(f"{name:18} | {nn:>4} {ise:>+7.3f} {oose:>+7.3f} {pm['pf']:>5.2f} "
              f"{pm['total']*100:>+7.0f}% {pm['mdd']*100:>5.1f}% | {dsum:>+7.1f}({dn}) {dwin:>3.0f}%")


if __name__ == "__main__":
    main()
