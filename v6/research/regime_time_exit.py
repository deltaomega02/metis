"""운영자 idea (2026-06-08) — held-time + regime-transition profit-take for shorts.

Refines the earlier regime-exit (regime_exit_test.py, rejected: OOS +0.086→-0.060)
by adding TWO conditions 운영자 specified:
  1. held >= HOLD_MIN bars (don't cut early — only after the move has had time)
  2. regime TRANSITION 강한하락(ADX>=STRONG) → 중간(ADX<WEAKEN_TO), i.e. ADX must have
     PEAKED in the strong band and then faded (not just an absolute low ADX)
  ...and only WHEN IN PROFIT. Losers keep the structural SL (never cut losers early).

Two flavors:
  replace  : regime-time exit REPLACES the fixed 2.5R TP (no fixed target)
  overlay  : mixed (fixed 2.5R) AND regime-time, whichever fires first  ← faithful to
             "강한 하락 지속이면 2.5R까지 타고, 식으면 수익 챙겨라"

Long side = trend exit (unchanged) in all. Baseline = mixed (current live).
Verdict on OOS + walk-forward, size discounted. Data decides — win% up with expR down
(cut winners / run losers = negative skew) is the trap to watch.

"""
from __future__ import annotations

import numpy as np

from edge_scan import precompute, signal_dir, COINS, EMA_S, DON_N, BB_N, VB_N
from exit_redesign import DATA
from validate_breakout import portfolio_sim
from backtest_trend_15m import FEE_RT


def trades(o, h, l, c, t, ind, dirs, *, flavor, hold_min, strong, weaken_to,
           profit_floor=0.0, R_full=2.5, atr_k=1.5, slip_bps=2, fund=0.0001):
    """flavor: 'mixed'(baseline) | 'replace' | 'overlay'. Returns (t_in,t_out,R_net,tag)
    tag = 'TP'/'SL'/'REGIME'/'TREND' for diagnostics."""
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
            sl = entry - rk; j = i + 1; expx = None; tag = "TREND"
            while j < n:
                if l[j] <= sl: expx = sl; tag = "SL"; break
                if c[j] < es[j]: expx = c[j]; tag = "TREND"; break
                j += 1
            if expx is None: break
            gr = (expx - entry) / entry / (rk / entry)
            out.append((int(t[i]), int(t[j]), gr - cost_R - (j - i) * 0.5 * fund / (rk / entry), tag))
            i = j + 1; continue
        # short
        sl = entry + rk; tp = entry - R_full * rk
        peak = ax[i]; j = i + 1; expx = None; tag = None
        while j < n:
            if h[j] >= sl: expx = sl; tag = "SL"; break
            peak = max(peak, ax[j])
            unreal_R = -(c[j] - entry) / entry / (rk / entry)   # profit in R units
            held = j - i
            regime_fire = (flavor in ("replace", "overlay") and held >= hold_min
                           and peak >= strong and ax[j] < weaken_to and unreal_R > profit_floor)
            if regime_fire:
                expx = c[j]; tag = "REGIME"; break
            if flavor in ("mixed", "overlay") and l[j] <= tp:
                expx = tp; tag = "TP"; break
            j += 1
        if expx is None: break
        gr = -(expx - entry) / entry / (rk / entry)
        out.append((int(t[i]), int(t[j]), gr - cost_R - (j - i) * 0.5 * fund / (rk / entry), tag))
        i = j + 1
    return out


def collect(**kw):
    out = []
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists(): continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += trades(o, h, l, c, t, ind, dirs, **kw)
    return out


def is_oos(trs):
    tr = sorted(trs); s = int(len(tr) * 0.7)
    return np.array([x[2] for x in tr[:s]]).mean(), np.array([x[2] for x in tr[s:]]).mean()


def wf(trs, folds=5):
    tr = sorted(trs)
    edges = np.linspace(tr[0][0], tr[-1][0] + 1, folds + 1)
    cells, pos = [], 0
    for k in range(folds):
        seg = [x[2] for x in tr if edges[k] <= x[0] < edges[k + 1]]
        if len(seg) >= 10:
            m = np.mean(seg); cells.append(f"{m:+.2f}"); pos += m > 0
        else:
            cells.append("  —")
    return " ".join(cells), pos, folds


def main():
    print("=== 운영자 보유시간+레짐전환(강한하락→중간) 수익익절 vs mixed · 8코인 4h 마찰후 risk0.75%/cap4 ===\n")
    base = collect(flavor="mixed", hold_min=0, strong=55, weaken_to=55)
    bi, bo = is_oos(base); bpm = portfolio_sim(sorted([x[:3] for x in base]), risk_frac=0.0075, cap=4)
    bw, bp, bf = wf(base)
    print(f"{'구성':38} | {'n':>4} {'IS':>7} {'OOS':>7} {'win':>4} {'PF':>5} {'total':>7} {'MDD':>5} | WF")
    print(f"{'mixed(현재 2.5R)':38} | {len(base):>4} {bi:>+7.3f} {bo:>+7.3f} "
          f"{(np.array([x[2] for x in base])>0).mean()*100:>3.0f}% {bpm['pf']:>5.2f} "
          f"{bpm['total']*100:>+6.0f}% {bpm['mdd']*100:>4.1f}% | {bw} ({bp}/{bf})")
    print()

    # 운영자 아이디어 강한형: overlay(2.5R 유지+레짐익절), 의미있는 수익(profit_floor)일 때만
    configs = []
    for hold_min in (1, 2):
        for weaken_to in (50, 45):
            for pf in (0.0, 1.0, 1.5):
                configs.append(("overlay", hold_min, weaken_to, pf))
    # replace(2.5R 버림) 대표 1개도 비교
    configs.append(("replace", 1, 45, 1.0))
    for flavor, hm, wt, pf in configs:
        trs = collect(flavor=flavor, hold_min=hm, strong=55, weaken_to=wt, profit_floor=pf)
        ii, oo = is_oos(trs); pm = portfolio_sim(sorted([x[:3] for x in trs]), risk_frac=0.0075, cap=4)
        wcells, wp, wf_n = wf(trs)
        win = (np.array([x[2] for x in trs]) > 0).mean() * 100
        nreg = sum(1 for x in trs if x[3] == "REGIME")
        name = f"{flavor:7} h>={hm} ADX55→<{wt} 수익>{pf:.1f}R"
        print(f"{name:38} | {len(trs):>4} {ii:>+7.3f} {oo:>+7.3f} {win:>3.0f}% {pm['pf']:>5.2f} "
              f"{pm['total']*100:>+6.0f}% {pm['mdd']*100:>4.1f}% | {wcells} ({wp}/{wf_n}) [reg{nreg}]")
    print("\n* baseline mixed OOS(+0.086)를 넘고 WF +folds 5/5여야 채택. [regN]=레짐익절 발동 횟수.")


if __name__ == "__main__":
    main()
