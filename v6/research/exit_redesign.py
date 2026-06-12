"""ls exit redesign — does a profit-protecting exit fix the short side?

The live 'trend' exit (ride until close crosses back through EMA50) never lets a
strong short realize: in a hard downtrend EMA50 sits far ABOVE entry, so the exit
only fires after price rebounds past entry — turning a big paper gain into a loss
(observed live 2026-06-05: 4 ls shorts at +4.8% paper would realize -4~-8%).

This compares profit-protecting exits against the baseline, separately for
long-only / short-only / both, with friction, IS/OOS split, walk-forward, and the
live 8-coin portfolio (risk 0.75%, cap 4). Verdict on SIGN (does short go +?),
size discounted, OOS-checked.

  trend    : ride until close crosses back through EMA50          (baseline/live)
  fixed    : take profit at R·risk                                 (fixed target)
  trail    : Chandelier — exit at best_price ∓ trail_k·ATR         (trail from entry)
  trail1R  : structural until +1R reached, then Chandelier trail   (let it run, then lock)

"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir, COINS, EMA_S, DON_N, BB_N, VB_N
from backtest_trend_15m import FEE_RT
from validate_breakout import portfolio_sim

DATA = Path(__file__).resolve().parent / "data"


def trades_coin(o, h, l, c, t, ind, dirs, *, atr_k, R, exit_mode, trail_k,
                slip_bps, fund_rate, side_filter, tf_h=4):
    """One coin's breakout trades under `exit_mode`. side_filter: +1 long, -1 short,
    0 both. Returns (t_in, t_out, R_net)."""
    cost = FEE_RT + 2 * slip_bps / 10000.0
    es, av = ind["es"], ind["av"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    out = []
    i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0 or (side_filter == 1 and di < 0) or (side_filter == -1 and di > 0):
            i += 1; continue
        entry = c[i]; rk = atr_k * av[i]
        if rk <= 0 or not np.isfinite(rk):
            i += 1; continue
        sl = entry - di * rk; tp = entry + di * R * rk
        best = entry                       # best favorable price so far
        trailing = (exit_mode == "trail")  # trail active from entry
        j = i + 1; expx = None
        while j < n:
            best = max(best, h[j]) if di == 1 else min(best, l[j])
            if exit_mode == "trail1R" and not trailing and di * (best - entry) >= rk:
                trailing = True            # +1R reached → start trailing
            tstop = best - di * trail_k * av[i] if trailing else None
            # 'mixed' = long rides the trend (EMA50), short takes the fixed R target
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if trailing and l[j] <= tstop: expx = tstop; break
                if exit_mode == "fixed" and h[j] >= tp: expx = tp; break
                if exit_mode in ("trend", "trail1R", "mixed") and not trailing and c[j] < es[j]: expx = c[j]; break
            else:
                if h[j] >= sl: expx = sl; break
                if trailing and h[j] >= tstop: expx = tstop; break
                if exit_mode in ("fixed", "mixed") and l[j] <= tp: expx = tp; break
                if exit_mode in ("trend", "trail1R") and not trailing and c[j] > es[j]: expx = c[j]; break
            j += 1
        if expx is None:
            break
        gross = di * (expx - entry) / entry
        net = gross - cost - (j - i) * (tf_h / 8.0) * fund_rate
        out.append((int(t[i]), int(t[j]), net / (rk / entry)))
        i = j + 1
    return out


def collect(side_filter, exit_mode, trail_k, *, atr_k=1.5, R=2.5, slip=2, fund=0.0001):
    out = []
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += trades_coin(o, h, l, c, t, ind, dirs, atr_k=atr_k, R=R, exit_mode=exit_mode,
                           trail_k=trail_k, slip_bps=slip, fund_rate=fund, side_filter=side_filter)
    return out


def is_oos(trades):
    """Time-ordered 70/30 split; raw expR each side (sign check, cap-agnostic)."""
    tr = sorted(trades)
    if len(tr) < 20:
        return None
    s = int(len(tr) * 0.70)
    Ri = np.array([x[2] for x in tr[:s]]); Ro = np.array([x[2] for x in tr[s:]])
    return Ri.mean(), Ro.mean(), len(tr)


SIDES = {1: "long", -1: "short", 0: "both"}
EXITS = [("trend", 0.0), ("fixed", 0.0), ("trail", 3.0), ("mixed", 0.0)]


def row(sf, em, tk, R=2.5):
    tr = collect(sf, em, tk, R=R)
    io = is_oos(tr); pm = portfolio_sim(tr, risk_frac=0.0075, cap=4)
    if not io or not pm:
        print(f"{SIDES[sf]:>6} {em:>7} {('R%.1f' % R):>5} | (표본부족)"); return
    ise, oose, nn = io
    print(f"{SIDES[sf]:>6} {em:>7} {('R%.1f' % R):>5} | {nn:>4} {ise:>+7.3f} {oose:>+8.3f} "
          f"{pm['pf']:>5.2f} {pm['total']*100:>+7.0f}% {pm['cagr']*100:>+6.1f}% {pm['mdd']*100:>5.1f}%")


def walk_forward(trades, label, folds=5):
    tr = sorted(trades)
    if len(tr) < folds * 10:
        print(f"  {label:>20}: 표본부족"); return
    edges = np.linspace(tr[0][0], tr[-1][0] + 1, folds + 1)
    cells = []
    for k in range(folds):
        seg = [x[2] for x in tr if edges[k] <= x[0] < edges[k + 1]]
        cells.append(f"{np.mean(seg):+.2f}" if len(seg) >= 10 else "  — ")
    pos = sum(1 for k in range(folds)
              for seg in [[x[2] for x in tr if edges[k] <= x[0] < edges[k + 1]]]
              if len(seg) >= 10 and np.mean(seg) > 0)
    print(f"  {label:>20}: " + " ".join(cells) + f"   (+folds {pos}/{folds})")


def main():
    print("=== exit 재설계 — 8코인 4h BREAKOUT, 마찰 2bp+펀딩, risk0.75% cap4 (현=both/trend) ===\n")
    print(f"{'side':>6} {'exit':>7} {'R':>5} | {'n':>4} {'IS_expR':>7} {'OOS_expR':>8} "
          f"{'PF':>5} {'total':>8} {'CAGR':>7} {'MDD':>6}")
    for sf in (1, -1, 0):
        for em, tk in EXITS:
            row(sf, em, tk)
        print()

    print("=== 숏 fixed R 스윕 (이익 익절 타깃) ===")
    for R in (1.5, 2.0, 2.5, 3.0):
        row(-1, "fixed", 0.0, R=R)
    print()

    print("=== both mixed (롱=trend, 숏=fixed) — 숏 R별 ===")
    for R in (1.5, 2.0, 2.5):
        row(0, "mixed", 0.0, R=R)
    print()

    print("=== walk-forward (fold별 raw expR, 견고성) ===")
    walk_forward(collect(0, "trend", 0.0), "both trend(현 라이브)")
    walk_forward(collect(0, "mixed", 0.0, R=2.0), "both mixed R2.0")
    walk_forward(collect(1, "trend", 0.0), "long trend")
    walk_forward(collect(-1, "fixed", 0.0, R=2.0), "short fixed R2.0")


if __name__ == "__main__":
    main()
