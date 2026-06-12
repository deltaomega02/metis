"""Full profile of the LONG-ONLY + BTC-uptrend-gate breakout strategy.

Sections:
  1. base edge: IS/OOS + walk-forward 5 folds
  2. portfolio equity (concurrent cap, risk sizing, friction) → CAGR/MDD/Sharpe
  3. Monte-Carlo bootstrap → P(loss), MDD distribution
  4. parameter robustness (atr_k × gate) — overfit check
  5. coin-subset OOS (all / majors / top)
  6. by-year expR — is it decaying?

Long-only because every realtime regime detector left shorts net-negative.
BTC regime realtime via EMA (no look-ahead).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backtest_trend_15m import FEE_RT, ema
from edge_scan import precompute, signal_dir

DATA = Path(__file__).resolve().parent / "data"
MAJ = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]


def bt_full(o, h, l, c, t, ind, dirs, atr_k, slip=2, fund=0.0001, tfh=4):
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
        sl = entry - di * risk; j = i + 1; expx = None
        while j < n:
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if c[j] < es[j]: expx = c[j]; break
            else:
                if h[j] >= sl: expx = sl; break
                if c[j] > es[j]: expx = c[j]; break
            j += 1
        if expx is None:
            break
        net = di * (expx - entry) / entry - cost - (j - i) * (tfh / 8) * fund
        out.append((int(t[i]), int(t[j]), net / (risk / entry), "L" if di == 1 else "S"))
        i = j + 1
    return out


def btc_regime(kind="ema20_50"):
    b = np.load(DATA / "BTCUSDT_4h.npz"); bt = b["t"].astype(np.int64); bc = b["c"]
    if kind == "ema20_50": arr = ema(bc, 20) > ema(bc, 50)
    elif kind == "ema50_200": arr = ema(bc, 50) > ema(bc, 200)
    elif kind == "px_ema200": arr = bc > ema(bc, 200)
    else: arr = ema(bc, 20) > ema(bc, 50)
    return bt, arr


def up_at(bt, arr, ts):
    i = int(np.searchsorted(bt, ts, side="right")) - 1
    return bool(arr[i]) if i >= 0 else True


def collect(coins, atr_k=1.5):
    out = []
    for coin in coins:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += [(coin, *x) for x in bt_full(o, h, l, c, t, ind, dirs, atr_k)]
    return out


def long_gated(trades, bt, arr):
    """keep long-only trades taken while BTC uptrend (realtime)."""
    return [(c, ti, to, r) for (c, ti, to, r, s) in trades if s == "L" and up_at(bt, arr, ti)]


def port_sim(tr, risk=0.005, cap=4):
    ev = []
    for k, (c, ti, to, r) in enumerate(tr):
        ev.append((ti, 1, k, r)); ev.append((to, 0, k, r))
    ev.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0; mdd = 0.0; openc = 0; taken = set(); R = []
    for ts, typ, k, r in ev:
        if typ == 1:
            if openc < cap: taken.add(k); openc += 1
        elif k in taken:
            eq *= (1 + risk * r); openc -= 1; R.append(r)
            peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    days = (max(x[2] for x in tr) - min(x[1] for x in tr)) / 1000 / 86400
    cagr = (eq ** (365 / days) - 1) if days > 30 and eq > 0 else float("nan")
    Ra = np.array(R)
    sh = float((risk * Ra).mean() / (risk * Ra).std() * np.sqrt(len(Ra) / (days / 365))) if len(Ra) > 1 and Ra.std() > 0 else 0
    return dict(n=len(R), total=eq - 1, cagr=cagr, mdd=mdd, sharpe=sh)


def main():
    np.random.seed(7)
    bt, arr = btc_regime("ema20_50")
    allt = collect(list({f.name[:-7] for f in DATA.glob("*_4h.npz")}))
    lg = long_gated(allt, bt, arr)
    R = np.array([r for _, _, _, r in lg])
    tsplit = sorted(ti for _, ti, _, _ in lg)[int(len(lg) * 0.70)]

    print("="*64)
    print("LONG-ONLY + BTC uptrend gate (ema20>50) · 23 coins · 4h · 2bps")
    print("="*64)

    # 1. base + walk-forward
    isR = np.array([r for _, ti, _, r in lg if ti < tsplit])
    osR = np.array([r for _, ti, _, r in lg if ti >= tsplit])
    edges = np.linspace(min(x[1] for x in lg), max(x[1] for x in lg) + 1, 6)
    folds = []
    for k in range(5):
        seg = [r for _, ti, _, r in lg if edges[k] <= ti < edges[k+1]]
        folds.append(np.mean(seg) if len(seg) >= 10 else None)
    print(f"\n[1] edge  n={len(R)}  IS expR={isR.mean():+.3f}  OOS expR={osR.mean():+.3f}")
    print("    walk-forward folds: " + " ".join(f"{f:+.2f}" if f else "-" for f in folds) +
          f"  ({sum(1 for f in folds if f and f>0)}/5 +)")

    # 2. portfolio
    print("\n[2] portfolio equity (cap·risk sweep)")
    for risk in (0.003, 0.005):
        for cap in (2, 4):
            m = port_sim(lg, risk, cap)
            print(f"    risk{risk*100:.1f}% cap{cap}: total{m['total']*100:+.0f}% CAGR{m['cagr']*100:+.1f}% MDD{m['mdd']*100:.0f}% Sharpe{m['sharpe']:.2f} (n{m['n']})")

    # 3. monte carlo
    finals, mdds = [], []
    for _ in range(5000):
        s = np.random.choice(R, len(R), replace=True)
        eq = np.cumprod(1 + 0.005 * s); pk = np.maximum.accumulate(eq)
        finals.append(eq[-1]-1); mdds.append(((pk-eq)/pk).max())
    finals, mdds = np.array(finals), np.array(mdds)
    print(f"\n[3] monte-carlo (bootstrap 5000, risk0.5%, cap-ignored)")
    print(f"    final: p5={np.percentile(finals,5)*100:+.0f}% p50={np.percentile(finals,50)*100:+.0f}% p95={np.percentile(finals,95)*100:+.0f}%")
    print(f"    MDD: p50={np.percentile(mdds,50)*100:.0f}% p95={np.percentile(mdds,95)*100:.0f}%   P(final<0)={ (finals<0).mean()*100:.1f}%")

    # 4. parameter robustness
    print("\n[4] parameter robustness (OOS expR) — 과적합이면 들쭉날쭉")
    for gk in ("ema20_50", "ema50_200", "px_ema200"):
        bt2, arr2 = btc_regime(gk)
        row = []
        for ak in (1.0, 1.5, 2.0):
            at = collect(list({f.name[:-7] for f in DATA.glob("*_4h.npz")}), atr_k=ak)
            lg2 = long_gated(at, bt2, arr2)
            os2 = [r for _, ti, _, r in lg2 if ti >= tsplit]
            row.append(np.mean(os2) if len(os2) >= 10 else float("nan"))
        print(f"    gate {gk:>10}: atr_k1.0={row[0]:+.3f} 1.5={row[1]:+.3f} 2.0={row[2]:+.3f}")

    # 5. coin subset OOS
    print("\n[5] coin subset (OOS expR)")
    for nm, coins in (("all23", None), ("major8", MAJ), ("BTC+ETH+SOL", ["BTCUSDT","ETHUSDT","SOLUSDT"])):
        sub = collect(coins) if coins else allt
        lgs = long_gated(sub, bt, arr)
        os3 = [r for _, ti, _, r in lgs if ti >= tsplit]
        if len(os3) >= 10:
            a = np.array(os3); gp = a[a>0].sum(); gl = -a[a<0].sum()
            print(f"    {nm:>12}: n={len(os3):>4} OOS expR={a.mean():+.3f} PF={gp/max(gl,1e-9):.2f}")

    # 6. by year
    print("\n[6] 연도별 expR (decay 점검)")
    byy = defaultdict(list)
    for _, ti, _, r in lg:
        byy[datetime.fromtimestamp(ti/1000, tz=timezone.utc).year].append(r)
    for y in sorted(byy):
        a = np.array(byy[y]); print(f"    {y}: n={len(a):>4} expR={a.mean():+.3f} win={(a>0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
