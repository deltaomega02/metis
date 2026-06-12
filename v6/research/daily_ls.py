"""Daily (1D) arm test in the DEPLOYED ls shape — 8 majors, BREAKOUT both ways,
mixed exit (long trend / short fixed 2.5R), atr_k 1.5, slip 2bp.

daily_scan.py said daily BREAKOUT OOS is weak (+0.052) — but that was 23 coins
with the plain trend exit (pre-mixed). This re-tests the exact deployed recipe at
1D: IS/OOS split, walk-forward 5 folds, standalone portfolio (cap 4, risk .75%),
and the diversification question — monthly-return correlation vs the live 4h arm.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from exit_redesign import trades_coin

DATA = Path(__file__).resolve().parent / "data"
MAJ = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
       "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]


def resample_d(o, h, l, c, t, f=6):
    n = (len(c) // f) * f
    return (o[:n].reshape(-1, f)[:, 0], h[:n].reshape(-1, f).max(1),
            l[:n].reshape(-1, f).min(1), c[:n].reshape(-1, f)[:, -1],
            t[:n].reshape(-1, f)[:, 0])


def collect(tf_h):
    out = []
    for coin in MAJ:
        d = np.load(DATA / f"{coin}_4h.npz")
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if tf_h == 24:
            o, h, l, c, t = resample_d(o, h, l, c, t)
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += trades_coin(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                           exit_mode="mixed", trail_k=2.0, slip_bps=2,
                           fund_rate=0.0001, side_filter=0, tf_h=tf_h)
    return sorted(out)


def portfolio(trades, risk_frac=0.0075, cap=4):
    events = []
    for k, (ti, to, R) in enumerate(trades):
        events.append((ti, 1, k, R))
        events.append((to, 0, k, R))
    events.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0
    mdd = 0.0
    openc = 0
    taken = set()
    curve = []  # (ts, eq) at closes
    for ts, typ, k, R in events:
        if typ == 1:
            if openc < cap:
                taken.add(k)
                openc += 1
        elif k in taken:
            eq *= (1 + risk_frac * R)
            openc -= 1
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
            curve.append((ts, eq))
    days = (max(x[1] for x in trades) - min(x[0] for x in trades)) / 1000 / 86400
    return dict(n=len(curve), total=eq - 1, mdd=mdd,
                cagr=(eq ** (365.0 / days) - 1) if days > 30 and eq > 0 else float("nan"),
                curve=curve)


def monthly_returns(curve):
    """Equity curve → {YYYY-MM: monthly log-ish return} for correlation."""
    from datetime import datetime, timezone
    out = {}
    last_eq = 1.0
    last_m = None
    for ts, eq in curve:
        m = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        if last_m is None:
            last_m = m
        if m != last_m:
            out[last_m] = eq / last_eq - 1
            last_eq = eq
            last_m = m
    return out


def stats(tag, trs):
    ti = [x[0] for x in trs]
    split = sorted(ti)[int(len(ti) * 0.70)]
    R = np.array([r for _, _, r in trs])
    Ro = np.array([r for t0, _, r in trs if t0 >= split])
    Lo = np.array([r for t0, _, r in trs if t0 >= split and r is not None])
    print(f"{tag}: n={len(R)} IS+OOS avgR={R.mean():+.3f} | OOS n={len(Ro)} avgR={Ro.mean():+.3f} "
          f"win={float((Ro>0).mean())*100:.0f}% PF={Ro[Ro>0].sum()/max(-Ro[Ro<0].sum(),1e-9):.2f}")
    # walk-forward 5 folds
    qs = np.quantile(ti, [0.2, 0.4, 0.6, 0.8])
    folds = []
    for a, b in zip([min(ti)] + list(qs), list(qs) + [max(ti) + 1]):
        fr = np.array([r for t0, _, r in trs if a <= t0 < b])
        folds.append(fr.mean() if len(fr) else float("nan"))
    print(f"  WF5: {' '.join(f'{x:+.3f}' for x in folds)}  ({sum(1 for x in folds if x>0)}/5 +)")
    return split


def main():
    d4 = collect(4)
    d1 = collect(24)
    print("=== 배포형 ls(mixed) 8메이저 ===")
    stats("4h(라이브)", d4)
    stats("1D(후보) ", d1)
    p4 = portfolio(d4)
    p1 = portfolio(d1)
    print(f"\n포트폴리오(cap4·risk0.75%): 4h total={p4['total']*100:+.0f}% CAGR={p4['cagr']*100:+.1f}% MDD={p4['mdd']*100:.1f}%")
    print(f"                          1D total={p1['total']*100:+.0f}% CAGR={p1['cagr']*100:+.1f}% MDD={p1['mdd']*100:.1f}%")
    m4 = monthly_returns(p4["curve"])
    m1 = monthly_returns(p1["curve"])
    common = sorted(set(m4) & set(m1))
    if len(common) > 12:
        a = np.array([m4[m] for m in common])
        b = np.array([m1[m] for m in common])
        rho = float(np.corrcoef(a, b)[0, 1])
        both_neg = float(np.mean((a < 0) & (b < 0)))
        print(f"\n월수익 상관(4h vs 1D, {len(common)}개월): ρ={rho:+.2f} · 동반손실월={both_neg*100:.0f}%")


if __name__ == "__main__":
    main()
