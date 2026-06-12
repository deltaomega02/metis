"""ls (long+short, mixed exit) by market regime — does it cope in up/down/range?

ls has only been validated live in one downtrend leg. This labels every 4h bar by
BTC regime and buckets each ls trade by the regime AT ENTRY, so we see whether ls
only earns in downtrends or also holds up in rallies (short side at risk) and chop
(breakout whipsaw at risk). Regime per BTC bar:
  range : ADX(14) < 22            (no trend — breakout fakeouts most likely)
  up    : ADX≥22 and ema50>ema200 (uptrend — short side should bleed)
  down  : ADX≥22 and ema50<ema200 (downtrend — where ls has earned live)

mixed exit = long rides trend (EMA50), short takes fixed 2.5R target.
"""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict

import numpy as np

from edge_scan import precompute, signal_dir, COINS
from exit_redesign import trades_coin, DATA
from validate_breakout import portfolio_sim
from backtest_trend_15m import ema, adx


def btc_regime_series():
    d = np.load(DATA / "BTCUSDT_4h.npz")
    h, l, c, t = d["h"], d["l"], d["c"], d["t"]
    e50 = ema(c, 50); e200 = ema(c, 200); ax = adx(h, l, c, 14)
    ts = [int(x) for x in t]; reg = []
    for i in range(len(c)):
        if not np.isfinite(ax[i]) or ax[i] < 22:
            reg.append("range")
        elif e50[i] > e200[i]:
            reg.append("up")
        else:
            reg.append("down")
    return ts, reg


def regime_at(ts, reg, t_in):
    i = bisect.bisect_right(ts, t_in) - 1
    return reg[i] if 0 <= i < len(reg) else "range"


def stat(Rs):
    Rs = np.array(Rs)
    if len(Rs) == 0:
        return "n=0"
    gp = Rs[Rs > 0].sum(); gl = -Rs[Rs < 0].sum()
    return (f"n={len(Rs):>4} expR={Rs.mean():>+.3f} win={(Rs>0).mean()*100:>3.0f}% "
            f"PF={gp/max(gl,1e-9):>4.2f} sumR={Rs.sum():>+7.1f}")


def main():
    ts, reg = btc_regime_series()
    print("=== BTC regime 분포 (4h봉, 5.9년) ===")
    cnt = Counter(reg); tot = sum(cnt.values())
    for r in ("up", "down", "range"):
        print(f"  {r:>6}: {cnt[r]:>5}봉 ({cnt[r]/tot*100:.0f}%)")

    # collect ls mixed trades, tag each with entry regime + side
    by = defaultdict(list)           # (regime, side) -> [R]
    regime_trades = defaultdict(list)  # regime -> [(ti,to,R)] for portfolio per regime
    for coin in COINS:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tl = [int(x) for x in t]
        for (ti, to, R) in trades_coin(o, h, l, c, t, ind, dirs, atr_k=1.5, R=2.5,
                                       exit_mode="mixed", trail_k=0.0, slip_bps=2,
                                       fund_rate=0.0001, side_filter=0):
            idx = bisect.bisect_left(tl, ti)
            side = "long" if (idx < len(dirs) and dirs[idx] > 0) else "short"
            rg = regime_at(ts, reg, ti)
            by[(rg, side)].append(R); by[(rg, "all")].append(R)
            regime_trades[rg].append((ti, to, R))

    print("\n=== ls mixed: regime × 방향 (raw R, 마찰 2bp+펀딩) ===")
    for rg in ("up", "down", "range"):
        print(f"[{rg}]")
        for side in ("all", "long", "short"):
            print(f"   {side:>5}: {stat(by[(rg, side)])}")

    print("\n=== regime별 포트폴리오 (risk0.75% cap4, 그 장세 트레이드만) ===")
    print(f"   {'regime':>6} | {'n':>4} {'win':>4} {'PF':>5} {'total':>8} {'MDD':>6}")
    for rg in ("up", "down", "range"):
        pm = portfolio_sim(sorted(regime_trades[rg]), risk_frac=0.0075, cap=4)
        if pm:
            print(f"   {rg:>6} | {pm['n']:>4} {pm['win']*100:>3.0f}% {pm['pf']:>5.2f} "
                  f"{pm['total']*100:>+7.0f}% {pm['mdd']*100:>5.1f}%")
        else:
            print(f"   {rg:>6} | 표본부족")

    # contrast: long-only vs ls in each regime (does shorting help or hurt per regime?)
    print("\n=== regime별 net sumR: 롱only vs 롱+숏(ls) ===")
    print(f"   {'regime':>6} | {'롱only_sumR':>11} {'ls_sumR':>9} {'숏기여':>8}")
    for rg in ("up", "down", "range"):
        lo = sum(by[(rg, "long")]); sh = sum(by[(rg, "short")]); ls = lo + sh
        print(f"   {rg:>6} | {lo:>+11.1f} {ls:>+9.1f} {sh:>+8.1f}")


if __name__ == "__main__":
    main()
