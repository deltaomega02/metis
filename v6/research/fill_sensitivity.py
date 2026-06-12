"""Intrabar fill sensitivity — is the 4h-candle backtest distorting hard-downtrend
shorts?

운영자's doubt: 4h OHLC doesn't know the intrabar price path. A short's SL (above) and
TP (below) can BOTH be touched within one 4h bar, and the backtest can't know which
came first — so it assumes 'SL first' (the pessimistic손절). If that assumption is
wrong often (likely in volatile downtrends), the backtest's negative short edge in
downtrends is an artifact, not reality, and live (real ticks) would differ.

This measures, per regime and SHORTS ONLY:
  - ambiguous% : share of shorts whose exit bar touched both SL and TP (path unknown)
  - sumR under fill = sl_first (pessimistic) / tp_first (optimistic) / mid
A big gap between sl_first and tp_first in downtrends ⇒ the candle data genuinely
can't判定 short edge there, and the live tick result is the one to trust.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from edge_scan import precompute, signal_dir, COINS, EMA_S, DON_N, BB_N, VB_N
from exit_redesign import DATA
from backtest_trend_15m import FEE_RT
from regime_ls import btc_regime_series, regime_at


def trades_fill(o, h, l, c, t, ind, dirs, *, fill, atr_k=1.5, R=2.5, slip_bps=2, fund=0.0001):
    """mixed exit (long=trend, short=fixed R). For shorts, when SL and TP touch in the
    same bar, resolve by `fill`. Returns (t_in, R_net, di, ambiguous)."""
    cost = FEE_RT + 2 * slip_bps / 10000.0
    es, av = ind["es"], ind["av"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    out = []; i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; rk = atr_k * av[i]
        if rk <= 0 or not np.isfinite(rk):
            i += 1; continue
        sl = entry - di * rk; tp = entry + di * R * rk
        j = i + 1; expx = None; amb = 0
        while j < n:
            if di == 1:                       # long: trend exit (no intrabar ambiguity vs TP)
                if l[j] <= sl: expx = sl; break
                if c[j] < es[j]: expx = c[j]; break
            else:                             # short: fixed R target vs SL
                sl_hit = h[j] >= sl; tp_hit = l[j] <= tp
                if sl_hit and tp_hit:
                    amb = 1
                    expx = sl if fill == "sl_first" else (tp if fill == "tp_first" else (sl + tp) / 2)
                    break
                if sl_hit: expx = sl; break
                if tp_hit: expx = tp; break
            j += 1
        if expx is None:
            break
        gross = di * (expx - entry) / entry
        net = gross - cost - (j - i) * (4 / 8.0) * fund
        out.append((int(t[i]), net / (rk / entry), di, amb))
        i = j + 1
    return out


def main():
    ts, reg = btc_regime_series()
    print("=== 봉내 체결 가정 민감도 — 숏만, regime별 (4h 캔들) ===\n")
    results = {}
    for fill in ("sl_first", "tp_first", "mid"):
        by = defaultdict(list); amb = defaultdict(lambda: [0, 0])
        for coin in COINS:
            f = DATA / f"{coin}_4h.npz"
            if not f.exists():
                continue
            d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
            ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
            for (ti, Rr, di, a) in trades_fill(o, h, l, c, t, ind, dirs, fill=fill):
                if di == -1:
                    rg = regime_at(ts, reg, ti)
                    by[rg].append(Rr); amb[rg][0] += a; amb[rg][1] += 1
        results[fill] = (by, amb)

    # ambiguous share (same across fills — count once)
    _, amb0 = results["sl_first"]
    print("regime별 숏 동시터치(봉내 SL&TP 둘 다) 비율:")
    for rg in ("up", "down", "range"):
        a, n = amb0[rg]
        print(f"   {rg:>6}: {a}/{n} = {a/max(n,1)*100:.0f}% 모호")

    print("\nregime별 숏 sumR — 가정별 (격차 클수록 캔들로 판정 불가):")
    print(f"   {'regime':>6} | {'SL우선(현재)':>12} {'TP우선':>9} {'중간':>8} {'격차':>8}")
    for rg in ("up", "down", "range"):
        s_sl = sum(results["sl_first"][0][rg])
        s_tp = sum(results["tp_first"][0][rg])
        s_md = sum(results["mid"][0][rg])
        print(f"   {rg:>6} | {s_sl:>+12.1f} {s_tp:>+9.1f} {s_md:>+8.1f} {s_tp-s_sl:>+8.1f}")


if __name__ == "__main__":
    main()
