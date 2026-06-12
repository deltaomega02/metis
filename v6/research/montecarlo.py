"""Monte-Carlo robustness of the BREAKOUT edge across all fetched coins.

Pools every BREAKOUT trade (net of 2bps slippage + funding) from all *_4h coins,
then bootstrap-resamples the trade set thousands of times to get DISTRIBUTIONS of
final return and max-drawdown at a fixed per-trade risk. This answers "is the
edge real or luck, and how bad is the unlucky case" rather than trusting one path.

"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from validate_breakout import backtest_fr

DATA = Path(__file__).resolve().parent / "data"
RISK_FRAC = 0.005
SIMS = 5000
np.random.seed(7)


def collect_all() -> tuple[np.ndarray, dict]:
    coins = sorted(f.name[:-7] for f in DATA.glob("*_4h.npz"))
    allR = []
    per = {}
    for coin in coins:
        d = np.load(DATA / f"{coin}_4h.npz")
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tr = backtest_fr(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                         exit_mode="trend", slip_bps=2, fund_rate=0.0001, tf_hours=4)
        rs = [x[2] for x in tr]
        allR += rs
        if len(rs) >= 10:
            a = np.array(rs)
            per[coin] = (len(rs), float(a.mean()), float(a[a > 0].sum() / max(-a[a < 0].sum(), 1e-9)))
    return np.array(allR), per


def main():
    R, per = collect_all()
    n = len(R)
    wins = (R > 0).mean()
    gp = R[R > 0].sum(); gl = -R[R < 0].sum()
    pf = gp / max(gl, 1e-9)
    print(f"coins={len(per)}  total trades={n}  win={wins*100:.1f}%  expR={R.mean():+.3f}  PF={pf:.2f}")
    print(f"per-coin expR>0: {sum(1 for v in per.values() if v[1] > 0)}/{len(per)}")

    finals, mdds = [], []
    for _ in range(SIMS):
        s = np.random.choice(R, n, replace=True)
        eq = np.cumprod(1 + RISK_FRAC * s)
        peak = np.maximum.accumulate(eq)
        finals.append(eq[-1] - 1)
        mdds.append(float(((peak - eq) / peak).max()))
    finals = np.array(finals); mdds = np.array(mdds)

    def pct(a, p): return float(np.percentile(a, p))
    print(f"\n── Bootstrap {SIMS} sims · risk {RISK_FRAC*100:.1f}%/trade (cap ignored, independent) ──")
    print(f"  final return:  p5={pct(finals,5)*100:+.0f}%  p50={pct(finals,50)*100:+.0f}%  p95={pct(finals,95)*100:+.0f}%")
    print(f"  max drawdown:  p50={pct(mdds,50)*100:.0f}%  p95={pct(mdds,95)*100:.0f}%  worst={mdds.max()*100:.0f}%")
    print(f"  P(final < 0): {(finals < 0).mean()*100:.1f}%")
    print(f"  P(final < -10%): {(finals < -0.10).mean()*100:.1f}%")

    print("\n── per-coin (n, expR, PF) ──")
    for coin in sorted(per, key=lambda k: per[k][1], reverse=True):
        nn, er, p = per[coin]
        print(f"  {coin:10} n={nn:4} expR={er:+.3f} PF={p:.2f}")


if __name__ == "__main__":
    main()
