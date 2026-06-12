"""Regime × strategy matrix: which strategy actually makes money in each market.

For 23 coins, run all 4 strategies (TREND/BREAKOUT/MEANREV/VOLBREAK), tag every
trade with the realtime BTC regime it opened in (up / down / chop via BTC EMA &
ADX), and report OUT-OF-SAMPLE expR per (regime × strategy), with long/short
split. The goal: find if every regime has a net-positive strategy (→ adaptive
'always trade, always up' is possible) or not (→ some regimes have no edge).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import FEE_RT, adx, ema
from edge_scan import precompute, signal_dir

DATA = Path(__file__).resolve().parent / "data"
STRATS = ["TREND", "BREAKOUT", "MEANREV", "VOLBREAK"]


def bt(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5, exit_mode="trend", slip=2, fund=0.0001, tfh=4):
    cost = FEE_RT + 2 * slip / 10000.0
    es, av = ind["es"], ind["av"]
    n = len(c); out = []; i = 60
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; risk = atr_k * av[i]
        if risk <= 0 or not np.isfinite(risk):
            i += 1; continue
        sl = entry - di * risk; tp = entry + di * R * risk; j = i + 1; expx = None
        while j < n:
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if exit_mode == "fixed" and h[j] >= tp: expx = tp; break
                if exit_mode == "trend" and c[j] < es[j]: expx = c[j]; break
            else:
                if h[j] >= sl: expx = sl; break
                if exit_mode == "fixed" and l[j] <= tp: expx = tp; break
                if exit_mode == "trend" and c[j] > es[j]: expx = c[j]; break
            j += 1
        if expx is None:
            break
        net = di * (expx - entry) / entry - cost - (j - i) * (tfh / 8) * fund
        out.append((int(t[i]), net / (risk / entry), "L" if di == 1 else "S"))
        i = j + 1
    return out


def btc_regime_series():
    b = np.load(DATA / "BTCUSDT_4h.npz"); bt_t = b["t"].astype(np.int64)
    h, l, c = b["h"], b["l"], b["c"]
    e50 = ema(c, 50); e200 = ema(c, 200); ax = adx(h, l, c, 14)
    reg = np.empty(len(c), dtype=object)
    for i in range(len(c)):
        if ax[i] < 22:
            reg[i] = "chop"
        else:
            reg[i] = "up" if e50[i] > e200[i] else "down"
    return bt_t, reg


def main():
    bt_t, reg = btc_regime_series()

    def reg_at(ts):
        i = int(np.searchsorted(bt_t, ts, side="right")) - 1
        return reg[i] if i >= 0 else "chop"

    coins = [f.name[:-7] for f in DATA.glob("*_4h.npz")]
    # exit mode per strategy: trend strategies ride, mean-reversion takes fixed R
    exit_by = {"TREND": "trend", "BREAKOUT": "trend", "VOLBREAK": "trend", "MEANREV": "fixed"}

    # collect: strat -> list of (t_in, R, side)
    data = {s: [] for s in STRATS}
    for coin in coins:
        d = np.load(DATA / f"{coin}_4h.npz"); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c)
        for s in STRATS:
            dirs = signal_dir(s, o, h, l, c, ind)
            data[s] += bt(o, h, l, c, t, ind, dirs, exit_mode=exit_by[s])

    all_t = sorted(x[0] for s in STRATS for x in data[s])
    split = all_t[int(len(all_t) * 0.70)]

    def cell(strat, regime, side=None, phase="OOS"):
        R = np.array([r for ti, r, sd in data[strat]
                      if reg_at(ti) == regime and (ti < split) == (phase == "IS")
                      and (side is None or sd == side)])
        return R

    print("OOS expR  (regime × strategy, 모든 방향)  — 양수 칸이 그 장세의 엣지\n")
    print(f"{'regime':>6} |" + "".join(f"{s:>11}" for s in STRATS))
    for rg in ("up", "down", "chop"):
        row = f"{rg:>6} |"
        for s in STRATS:
            R = cell(s, rg)
            row += f"{(R.mean() if len(R)>=15 else float('nan')):>+8.3f}({len(R):>3})"[:11]
        print(row)

    print("\n방향 분리 (OOS expR, n) — 각 칸 롱/숏:")
    for rg in ("up", "down", "chop"):
        print(f"  [{rg}]")
        for s in STRATS:
            L = cell(s, rg, "L"); S = cell(s, rg, "S")
            ls = f"롱 {L.mean():+.3f}({len(L)})" if len(L) >= 15 else "롱 -"
            ss = f"숏 {S.mean():+.3f}({len(S)})" if len(S) >= 15 else "숏 -"
            print(f"     {s:>9}: {ls:<20} {ss}")


if __name__ == "__main__":
    main()
