"""Proper range/box mean-reversion test for chop markets.

Only in a quiet range (coin ADX < adx_max): buy near the box bottom on a bounce,
sell near the box top on a rejection; take profit at box mid, stop just outside
the box (box-break = thesis dead). Tests if a real box strategy has net edge on
chop, IS vs OOS, across 23 coins — the missing piece for "always trade".
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import FEE_RT, adx, atr

DATA = Path(__file__).resolve().parent / "data"


def roll(a, n, fn):
    out = np.full_like(a, np.nan, dtype=float)
    for i in range(n, len(a)):
        out[i] = fn(a[i - n:i])
    return out


def box_trades(o, h, l, c, t, *, box_n=40, adx_max=20, edge=0.18, slip=2, fund=0.0001, tfh=4):
    cost = FEE_RT + 2 * slip / 10000.0
    ax = adx(h, l, c, 14); av = atr(h, l, c, 14)
    dhi = roll(h, box_n, np.max); dlo = roll(l, box_n, np.min)
    n = len(c); out = []; i = box_n + 5
    while i < n - 1:
        if not np.isfinite(dhi[i]) or ax[i] >= adx_max:
            i += 1; continue
        box = dhi[i] - dlo[i]
        if box <= 0:
            i += 1; continue
        mid = (dhi[i] + dlo[i]) / 2
        lo_zone = dlo[i] + edge * box; hi_zone = dhi[i] - edge * box
        di = 0
        if c[i] <= lo_zone and c[i] > o[i]:
            di = 1; entry = c[i]; sl = dlo[i] - 0.3 * av[i]; tp = mid
        elif c[i] >= hi_zone and c[i] < o[i]:
            di = -1; entry = c[i]; sl = dhi[i] + 0.3 * av[i]; tp = mid
        if di == 0:
            i += 1; continue
        risk = abs(entry - sl)
        if risk <= 0:
            i += 1; continue
        j = i + 1; expx = None
        while j < n:
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if h[j] >= tp: expx = tp; break
            else:
                if h[j] >= sl: expx = sl; break
                if l[j] <= tp: expx = tp; break
            j += 1
        if expx is None:
            break
        net = di * (expx - entry) / entry - cost - (j - i) * (tfh / 8) * fund
        out.append((int(t[i]), net / (risk / entry), "L" if di == 1 else "S"))
        i = j + 1
    return out


def main():
    coins = [f.name[:-7] for f in DATA.glob("*_4h.npz")]
    for box_n, adx_max, edge in [(40, 20, 0.18), (30, 18, 0.15), (50, 22, 0.20)]:
        allt = []
        for coin in coins:
            d = np.load(DATA / f"{coin}_4h.npz"); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
            if len(c) < 260:
                continue
            allt += box_trades(o, h, l, c, t, box_n=box_n, adx_max=adx_max, edge=edge)
        if len(allt) < 30:
            print(f"box_n={box_n} adx<{adx_max} edge={edge}: n={len(allt)} 너무적음"); continue
        ts = sorted(x[0] for x in allt); split = ts[int(len(ts) * 0.70)]
        R = np.array([r for _, r, _ in allt])
        isR = np.array([r for ti, r, _ in allt if ti < split])
        osR = np.array([r for ti, r, _ in allt if ti >= split])
        L = np.array([r for _, r, s in allt if s == "L"]); S = np.array([r for _, r, s in allt if s == "S"])
        def pf(a): g = a[a > 0].sum(); return g / max(-a[a < 0].sum(), 1e-9)
        print(f"box_n={box_n} adx<{adx_max} edge={edge}: n={len(allt)} "
              f"IS expR={isR.mean():+.3f} OOS expR={osR.mean():+.3f}(PF{pf(osR):.2f}) "
              f"win={(R>0).mean()*100:.0f}% | 롱 {L.mean():+.3f}({len(L)}) 숏 {S.mean():+.3f}({len(S)})")


if __name__ == "__main__":
    main()
