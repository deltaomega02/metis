"""Coin-selection check: is picking 'best' coins an edge or overfitting?

For each coin, collect BREAKOUT trades (2bps), split the whole timeline 70/30.
Compare coin SETS by in-sample(IS) vs out-of-sample(OOS) expectancy:
  - all23      : everything
  - major8     : the current v6 set
  - core5      : highest-liquidity majors (reason-based, not return-ranked)
  - IS_best8   : the 8 coins with best IN-SAMPLE expR (the overfitting trap)
If IS_best8 looks great in-sample but collapses out-of-sample while reason-based
sets stay stable, that is the proof that 'optimize the combo' is curve fitting.

"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from validate_breakout import backtest_fr

DATA = Path(__file__).resolve().parent / "data"
MAJOR8 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
CORE5 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]


def collect():
    coin_tr = {}
    for f in sorted(DATA.glob("*_4h.npz")):
        coin = f.name[:-7]
        d = np.load(f)
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        coin_tr[coin] = backtest_fr(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                                    exit_mode="trend", slip_bps=2, fund_rate=0.0001, tf_hours=4)
    return coin_tr


def phase_stats(coins, coin_tr, split_ts, phase):
    R = [r for c in coins for (ti, to, r) in coin_tr.get(c, [])
         if (ti < split_ts) == (phase == "IS")]
    if len(R) < 10:
        return None
    R = np.array(R)
    gp = R[R > 0].sum(); gl = -R[R < 0].sum()
    return dict(n=len(R), expR=float(R.mean()), pf=float(gp / max(gl, 1e-9)), win=float((R > 0).mean()))


def main():
    coin_tr = collect()
    all_t = sorted(ti for tr in coin_tr.values() for (ti, _, _) in tr)
    split_ts = all_t[int(len(all_t) * 0.70)]

    # rank coins by IN-SAMPLE expR (this is the overfit selection)
    is_rank = []
    for c, tr in coin_tr.items():
        r = [x[2] for x in tr if x[0] < split_ts]
        if len(r) >= 10:
            is_rank.append((c, float(np.mean(r))))
    is_rank.sort(key=lambda x: x[1], reverse=True)
    is_best8 = [c for c, _ in is_rank[:8]]

    sets = {
        "all23": list(coin_tr.keys()),
        "major8 (현 v6)": MAJOR8,
        "core5 (유동성)": CORE5,
        "IS_best8 (과적합)": is_best8,
    }
    print(f"coins={len(coin_tr)}  split at 70%  (IS=과거, OOS=최근 30%)\n")
    print(f"{'set':>20} | {'IS expR':>8} {'IS PF':>6} | {'OOS expR':>8} {'OOS PF':>6} {'OOS n':>6}")
    for name, coins in sets.items():
        a = phase_stats(coins, coin_tr, split_ts, "IS")
        b = phase_stats(coins, coin_tr, split_ts, "OOS")
        if a and b:
            print(f"{name:>20} | {a['expR']:>+8.3f} {a['pf']:>6.2f} | "
                  f"{b['expR']:>+8.3f} {b['pf']:>6.2f} {b['n']:>6}")
    print(f"\nIS_best8 = {is_best8}")


if __name__ == "__main__":
    main()
