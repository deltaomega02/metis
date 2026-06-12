"""Risk-per-trade sweep for the DEPLOYED ls model (long+short, no gate, mixed exit).

The old risk_sweep.py profiled the long-only + BTC-gate variant; this sweeps the
arm actually live since 2026-06-08: 8 majors, BREAKOUT dirs both ways, mixed exit
(long rides EMA50 trend, short fixed 2.5R), atr_k 1.5, slip 2bp, cap 4, lev 3.

For risk ∈ {0.5, 0.75, 1.0, 1.25, 1.5}%:
  - full-history portfolio (cap 4) → total return / CAGR / MDD / Sharpe-ish
  - OOS-only portfolio (last 30% by entry time) — does higher risk hold OOS?
  - Monte-Carlo bootstrap (5000) → P(final<start), MDD p50/p95
  - margin feasibility at cap 4 / lev 3 (worst-case concurrent margin vs equity)
Risk%% scales qty only — stop distance vs liquidation band is leverage's domain
(unchanged at lev 3), so ruin here means equity bleed, not liquidation.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from exit_redesign import trades_coin

DATA = Path(__file__).resolve().parent / "data"
MAJ = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
       "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
CAP = 4
LEV = 3
RISKS = [0.005, 0.0075, 0.01, 0.0125, 0.015]


def collect_ls():
    """Deployed-config ls trades on the 8 majors → [(t_in, t_out, R_net, stop_pct)]."""
    out = []
    for coin in MAJ:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f)
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        trs = trades_coin(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                          exit_mode="mixed", trail_k=2.0, slip_bps=2,
                          fund_rate=0.0001, side_filter=0, tf_h=4)
        # stop% per trade for margin math: re-walk entries to grab atr at entry
        av = ind["av"]
        ti_to_stop = {}
        n = len(c)
        i = max(50, 20, 20, 20) + 5
        for (ti, to, R) in trs:
            idx = int(np.searchsorted(t.astype(np.int64), ti))
            if 0 <= idx < n and c[idx] > 0 and np.isfinite(av[idx]):
                ti_to_stop[ti] = 1.5 * av[idx] / c[idx]
        out += [(ti, to, R, ti_to_stop.get(ti, np.nan)) for (ti, to, R) in trs]
    return sorted(out)


def portfolio(trades, risk_frac, cap=CAP):
    """Event-driven compounding equity with concurrent cap. Returns metrics+series."""
    events = []
    for k, (ti, to, R, _sp) in enumerate(trades):
        events.append((ti, 1, k, R))
        events.append((to, 0, k, R))
    events.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0
    mdd = 0.0
    openc = 0
    taken = set()
    realized = []
    for ts, typ, k, R in events:
        if typ == 1:
            if openc < cap:
                taken.add(k)
                openc += 1
        elif k in taken:
            eq *= (1 + risk_frac * R)
            openc -= 1
            realized.append(R)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
    if len(realized) < 10:
        return None
    Rs = np.array(realized)
    days = (max(x[1] for x in trades) - min(x[0] for x in trades)) / 1000 / 86400
    return dict(n=len(realized), total=eq - 1, mdd=mdd,
                cagr=(eq ** (365.0 / days) - 1) if days > 30 and eq > 0 else float("nan"),
                win=float((Rs > 0).mean()), avgR=float(Rs.mean()), Rs=Rs)


def montecarlo(Rs, risk_frac, n_boot=5000, seed=7):
    rng = np.random.default_rng(seed)
    finals = np.empty(n_boot)
    mdds = np.empty(n_boot)
    n = len(Rs)
    for b in range(n_boot):
        path = 1 + risk_frac * Rs[rng.integers(0, n, n)]
        eq = np.cumprod(path)
        peak = np.maximum.accumulate(eq)
        finals[b] = eq[-1]
        mdds[b] = ((peak - eq) / peak).max()
    return dict(p_loss=float((finals < 1).mean()),
                mdd_p50=float(np.percentile(mdds, 50)),
                mdd_p95=float(np.percentile(mdds, 95)),
                fin_p05=float(np.percentile(finals, 5)))


def main():
    trades = collect_ls()
    all_ti = [x[0] for x in trades]
    split = sorted(all_ti)[int(len(all_ti) * 0.70)]
    oos = [x for x in trades if x[0] >= split]
    sps = np.array([x[3] for x in trades if np.isfinite(x[3])])
    print(f"ls(롱+숏 mixed) 8메이저 · trades={len(trades)} (OOS {len(oos)}) · "
          f"stop%% p50={np.percentile(sps,50)*100:.2f} p05={np.percentile(sps,5)*100:.2f}\n")
    hdr = (f"{'risk':>6} | {'n':>4} {'total':>9} {'CAGR':>7} {'MDD':>6} {'avgR':>6} | "
           f"{'OOS tot':>8} {'OOS MDD':>7} | {'P(loss)':>7} {'MDDp50':>6} {'MDDp95':>6} | {'margin@4':>8}")
    print(hdr)
    print("-" * len(hdr))
    for rf in RISKS:
        p = portfolio(trades, rf)
        po = portfolio(oos, rf)
        mc = montecarlo(p["Rs"], rf)
        # worst-case margin: 4 concurrent at p05 stop% → notional=risk/stop, margin=notional/lev
        m4 = 4 * (rf / np.percentile(sps, 5)) / LEV
        print(f"{rf*100:>5.2f}% | {p['n']:>4} {p['total']*100:>+8.0f}% {p['cagr']*100:>+6.1f}% "
              f"{p['mdd']*100:>5.1f}% {p['avgR']:>+6.3f} | "
              f"{(po['total']*100 if po else float('nan')):>+7.1f}% {(po['mdd']*100 if po else float('nan')):>6.1f}% | "
              f"{mc['p_loss']*100:>6.2f}% {mc['mdd_p50']*100:>5.1f}% {mc['mdd_p95']*100:>5.1f}% | "
              f"{m4*100:>7.1f}%")
    print("\nmargin@4 = 최악(스탑 좁은 p05) 4동시 마진/자본 — 100% 넘으면 잔고가드가 진입 스킵")


if __name__ == "__main__":
    main()
