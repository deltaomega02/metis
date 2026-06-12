"""v6 edge-proof backtest — does 15m TREND-FOLLOWING clear fees on SOL/ETH?

Encodes the research conclusion as an explicit, fee-aware rule set:
  - REGIME filter: trade only when ADX(14) > adx_min  → "trending", else stand aside.
  - TREND gate:    EMA_fast vs EMA_slow defines the only allowed direction
                   (uptrend → long-only, downtrend → short-only). No counter-trend.
  - ENTRY:         trend-direction pullback-continuation — price pulls back to
                   EMA_fast then resumes in the trend direction (close back
                   through EMA_fast with a trend-direction bar).
  - EXIT:          structural SL (entry ∓ k·ATR) + fixed TP at R·risk. SL/TP only,
                   no time-stop, no trailing (운영자 rule). First-touch, SL-priority
                   on an ambiguous bar (conservative).
  - COST:          0.11% round-trip taker fee subtracted from every trade's return.

Reports expectancy in R (NET of fees), win%, profit factor, trades/day, MDD —
split into IN-SAMPLE (first 70%) and OUT-OF-SAMPLE (last 30%) so a result that
only works in-sample is exposed. Counter-trend is never taken by construction:
the question is purely "does trend-aligned 15m entry have net edge".

"""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
FEE_RT = 0.0011  # 0.11% round-trip taker


# ── indicators (numpy) ──────────────────────────────────────────────
def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x)
    out[0] = x[0]
    a = 1 / n
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h, l, c, n=14):
    pc = np.empty_like(c); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return _wilder(tr, n)


def adx(h, l, c, n=14):
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm = np.concatenate([[0.0], plus_dm])
    minus_dm = np.concatenate([[0.0], minus_dm])
    tr = atr(h, l, c, 1)  # true range series (n=1 → raw TR)
    atr_n = _wilder(tr, n)
    pdi = 100 * _wilder(plus_dm, n) / np.maximum(atr_n, 1e-9)
    mdi = 100 * _wilder(minus_dm, n) / np.maximum(atr_n, 1e-9)
    dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    return _wilder(dx, n)


# ── backtest core ───────────────────────────────────────────────────
def run(o, h, l, c, *, ema_f, ema_s, adx_min, atr_k, R, atr_n=14, exit_mode="fixed_tp"):
    ef = ema(c, ema_f)
    es = ema(c, ema_s)
    av = atr(h, l, c, atr_n)
    ax = adx(h, l, c, 14)
    n = len(c)
    warm = max(ema_s, atr_n, 30) + 5

    trades = []  # each: (entry_idx, exit_idx, dir, ret_net, R_realized)
    i = warm
    while i < n - 1:
        trending = ax[i] > adx_min
        if not trending:
            i += 1; continue
        up = ef[i] > es[i]
        dn = ef[i] < es[i]
        # pullback-continuation: prior bar touched EMA_fast from trend side,
        # current bar resumes in trend direction through EMA_fast.
        long_sig = (up and l[i] <= ef[i] and c[i] > ef[i] and c[i] > o[i])
        short_sig = (dn and h[i] >= ef[i] and c[i] < ef[i] and c[i] < o[i])
        if not (long_sig or short_sig):
            i += 1; continue

        direction = 1 if long_sig else -1
        entry = c[i]
        risk = atr_k * av[i]
        if risk <= 0:
            i += 1; continue
        if direction == 1:
            sl = entry - risk; tp = entry + R * risk
        else:
            sl = entry + risk; tp = entry - R * risk

        # walk forward to exit. fixed_tp: first SL/TP touch (SL-priority).
        # trend: structural SL, else ride until trend terminates (close crosses
        # back through slow EMA) — let winners run, no fixed TP.
        exit_idx = None; exit_px = None
        j = i + 1
        while j < n:
            if direction == 1:
                if l[j] <= sl:
                    exit_idx, exit_px = j, sl; break
                if exit_mode == "fixed_tp" and h[j] >= tp:
                    exit_idx, exit_px = j, tp; break
                if exit_mode == "trend" and c[j] < es[j]:
                    exit_idx, exit_px = j, c[j]; break
            else:
                if h[j] >= sl:
                    exit_idx, exit_px = j, sl; break
                if exit_mode == "fixed_tp" and l[j] <= tp:
                    exit_idx, exit_px = j, tp; break
                if exit_mode == "trend" and c[j] > es[j]:
                    exit_idx, exit_px = j, c[j]; break
            j += 1
        if exit_idx is None:
            break  # ran out of data with position open → stop

        gross = direction * (exit_px - entry) / entry
        ret_net = gross - FEE_RT
        R_real = ret_net / (risk / entry)
        trades.append((i, exit_idx, direction, ret_net, R_real))
        i = exit_idx + 1  # single position, re-arm after close

    return trades


def metrics(trades, n_bars):
    if not trades:
        return dict(n=0)
    rets = np.array([t[3] for t in trades])
    Rs = np.array([t[4] for t in trades])
    wins = rets > 0
    gp = rets[rets > 0].sum()
    gl = -rets[rets < 0].sum()
    # equity curve (compounded, 1x notional, full-risk-per-trade proxy)
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(eq)
    mdd = float(((peak - eq) / peak).max()) if len(eq) else 0.0
    days = n_bars * 15 / 60 / 24
    return dict(
        n=len(trades),
        win=float(wins.mean()),
        exp_R=float(Rs.mean()),          # NET expectancy per trade in R
        exp_ret=float(rets.mean()),      # NET expectancy per trade in %
        pf=float(gp / gl) if gl > 0 else float("inf"),
        total=float(eq[-1] - 1),
        mdd=mdd,
        tpd=len(trades) / days,
    )


def fmt(m):
    if m.get("n", 0) == 0:
        return "no trades"
    return (f"n={m['n']:4d} win={m['win']*100:4.1f}% expR={m['exp_R']:+.3f} "
            f"exp%={m['exp_ret']*100:+.3f} PF={m['pf']:.2f} tot={m['total']*100:+6.1f}% "
            f"MDD={m['mdd']*100:4.1f}% t/day={m['tpd']:.2f}")


def resample(o, h, l, c, factor):
    """Aggregate 15m bars up by `factor` (4→1h, 16→4h). o=first,h=max,l=min,c=last."""
    if factor == 1:
        return o, h, l, c
    n = (len(c) // factor) * factor
    O = o[:n].reshape(-1, factor)[:, 0]
    H = h[:n].reshape(-1, factor).max(1)
    L = l[:n].reshape(-1, factor).min(1)
    C = c[:n].reshape(-1, factor)[:, -1]
    return O, H, L, C


def sweep(sym, exit_mode, grid, factor=1, suffix="15m"):
    d = np.load(DATA / f"{sym}_{suffix}.npz")
    o, h, l, c = resample(d["o"], d["h"], d["l"], d["c"], factor)
    split = int(len(c) * 0.70)
    results = []
    for (ef, es), adxm, ak, R in grid:
        tr_is = run(o[:split], h[:split], l[:split], c[:split],
                    ema_f=ef, ema_s=es, adx_min=adxm, atr_k=ak, R=R, exit_mode=exit_mode)
        tr_oos = run(o[split:], h[split:], l[split:], c[split:],
                     ema_f=ef, ema_s=es, adx_min=adxm, atr_k=ak, R=R, exit_mode=exit_mode)
        results.append(((ef, es, adxm, ak, R), metrics(tr_is, split), metrics(tr_oos, len(c) - split)))
    results.sort(key=lambda r: (r[1].get("exp_R", -9) if r[1].get("n", 0) >= 20 else -9), reverse=True)
    print(f"  ----- {sym}  [{exit_mode}]  ({len(c)} bars, IS={split}/OOS={len(c)-split}) -----")
    print(f"  {'ema':>7} {'adx':>3} {'atrk':>4} {'R':>3} | IN-SAMPLE                                              | OUT-OF-SAMPLE")
    for params, m_is, m_oos in results[:6]:
        ef, es, adxm, ak, R = params
        print(f"  {ef:>2}/{es:<3} {adxm:>3} {ak:>4} {R:>3} | {fmt(m_is):<54} | {fmt(m_oos)}")
    arr = np.array([r[2]["exp_R"] for r in results if r[2].get("n", 0) >= 20])
    best_oos = max((r[2]["exp_R"] for r in results if r[2].get("n", 0) >= 20), default=None)
    if len(arr):
        print(f"  OOS expR across {len(arr)} combos(n>=20): mean={arr.mean():+.3f} "
              f"median={np.median(arr):+.3f}  best={best_oos:+.3f}  >0 in {int((arr>0).sum())}/{len(arr)}")
    # STABILITY: combos where BOTH IS and OOS are net-positive with enough trades
    nmin = 30
    both = [(p, a, b) for p, a, b in results
            if a.get("n", 0) >= nmin and b.get("n", 0) >= nmin and a["exp_R"] > 0 and b["exp_R"] > 0]
    print(f"  STABLE (IS>0 & OOS>0, n>={nmin} both): {len(both)}/{sum(1 for p,a,b in results if a.get('n',0)>=nmin and b.get('n',0)>=nmin)}")
    for p, a, b in both[:5]:
        ef, es, adxm, ak, R = p
        print(f"    {ef}/{es} adx{adxm} k{ak} R{R}: IS expR={a['exp_R']:+.3f}(n{a['n']},PF{a['pf']:.2f}) "
              f"OOS expR={b['exp_R']:+.3f}(n{b['n']},PF{b['pf']:.2f},tot{b['total']*100:+.0f}%)")
    print()


def main():
    grid = list(itertools.product(
        [(20, 50), (20, 100)], [18, 22, 25], [1.0, 1.5], [1.5, 2.0, 2.5],
    ))
    print(f"FEE round-trip = {FEE_RT*100:.2f}%   grid={len(grid)} combos/sym")
    print("Exit = trend (let winners run). TIMEFRAME = 4h, multi-year, native bars.\n")
    for mode in ("trend", "fixed_tp"):
        print(f"################  EXIT = {mode}  ################")
        for sym in ("SOLUSDT", "ETHUSDT"):
            sweep(sym, mode, grid, factor=1, suffix="4h")


if __name__ == "__main__":
    main()
