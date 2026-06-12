"""운영자 idea — exit a winning short when the trend weakens (regime transition).

Instead of riding to a fixed 2.5R, close the short the moment its trend strength
(ADX) drops below a threshold WHILE in profit — capturing the gain as the downtrend
fades into chop. Loss side keeps the structural SL (you don't cut losers early, only
winners). Compares vs mixed. Watch for the classic trap: cutting winners early while
letting losers run to SL = negative skew (win% up, expR down). Data decides.

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


def trades(o, h, l, c, t, ind, dirs, *, mode, adx_exit=35.0, R_full=2.5,
           atr_k=1.5, slip_bps=2, fund=0.0001):
    """short exit modes: 'mixed'=fixed R_full | 'regime'=close in-profit when ADX<adx_exit
    (no fixed TP) | 'mixedregime'=whichever of (fixed TP / regime-exit) hits first.
    Long = trend exit in all. Returns (t_in,t_out,R_net)."""
    cost = FEE_RT + 2 * slip_bps / 10000.0
    es, av, ax = ind["es"], ind["av"], ind["ax"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    out = []; i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; rk = atr_k * av[i]
        if rk <= 0 or not np.isfinite(rk):
            i += 1; continue
        cost_R = cost / (rk / entry)
        if di == 1:                                # long: trend exit (unchanged)
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
        sl = entry + rk; tp = entry - R_full * rk; j = i + 1; expx = None
        while j < n:
            if h[j] >= sl: expx = sl; break                      # SL always
            unreal = -(c[j] - entry)                             # >0 means in profit (price down)
            if mode in ("regime", "mixedregime") and ax[j] < adx_exit and unreal > 0:
                expx = c[j]; break                               # trend weakened + winning → take it
            if mode in ("mixed", "mixedregime") and l[j] <= tp:
                expx = tp; break                                 # fixed R target
            j += 1
        if expx is None: break
        gr = -(expx - entry) / entry / (rk / entry)
        out.append((int(t[i]), int(t[j]), gr - cost_R - (j - i) * 0.5 * fund / (rk / entry)))
        i = j + 1
    return out


def collect(mode, **kw):
    out = []
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists(): continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += trades(o, h, l, c, t, ind, dirs, mode=mode, **kw)
    return out


def is_oos(trs):
    tr = sorted(trs); s = int(len(tr) * 0.7)
    return np.array([x[2] for x in tr[:s]]).mean(), np.array([x[2] for x in tr[s:]]).mean()


def main():
    print("=== 운영자 regime-exit (숏: ADX 약화+수익시 익절) vs mixed · 8코인 4h 마찰후 ===\n")
    print(f"{'구성':24} | {'n':>4} {'IS':>7} {'OOS':>7} {'win':>4} {'PF':>5} {'total':>8} {'MDD':>6}")
    configs = [
        ("mixed(현재) 2.5R",        dict(mode="mixed")),
        ("regime ADX<35 익절",      dict(mode="regime", adx_exit=35)),
        ("regime ADX<25 익절",      dict(mode="regime", adx_exit=25)),
        ("regime ADX<22(횡보) 익절", dict(mode="regime", adx_exit=22)),
        ("mixed+regime ADX<30",     dict(mode="mixedregime", adx_exit=30)),
    ]
    for name, kw in configs:
        trs = collect(**kw)
        ise, oose = is_oos(trs)
        pm = portfolio_sim(sorted(trs), risk_frac=0.0075, cap=4)
        Rs = np.array([x[2] for x in trs]); win = (Rs > 0).mean() * 100
        print(f"{name:24} | {len(trs):>4} {ise:>+7.3f} {oose:>+7.3f} {win:>3.0f}% {pm['pf']:>5.2f} "
              f"{pm['total']*100:>+7.0f}% {pm['mdd']*100:>5.1f}%")


if __name__ == "__main__":
    main()
