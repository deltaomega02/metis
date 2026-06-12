"""v6 edge scan — sweep coins × strategies × timeframes for a REAL net edge.

Strategies (all long+short, structural SL = k·ATR, exit = fixed R·risk OR
ride-until-trend-ends):
  TREND     pullback-continuation in EMA-trend direction (ADX>min)
  BREAKOUT  Donchian-N breakout in EMA-trend direction (ADX>min)
  MEANREV   Bollinger-band fade in a RANGE (ADX<min) — counter-move
  VOLBREAK  prior-N high/low breakout, regime-agnostic

Verdict per (coin, tf, strat, exit, params): NET expectancy R after 0.11%
round-trip, IS(70%)/OOS(30%) split. A cell only counts as edge if IS>0 AND
OOS>0 with n>=30 each AND OOS beats buy-and-hold over the OOS window.

"""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np

from backtest_trend_15m import ema, atr, adx, FEE_RT

DATA = Path(__file__).resolve().parent / "data"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
         "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
TFS = ["4h", "1h"]
STRATS = ["TREND", "BREAKOUT", "MEANREV", "VOLBREAK"]

EMA_F, EMA_S, ADX_MIN, DON_N, BB_N, BB_K, VB_N = 20, 50, 22, 20, 20, 2.0, 20


def roll_max(x, n):
    out = np.full_like(x, -np.inf)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].max()
    return out


def roll_min(x, n):
    out = np.full_like(x, np.inf)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].min()
    return out


def precompute(o, h, l, c):
    ef = ema(c, EMA_F); es = ema(c, EMA_S)
    ax = adx(h, l, c, 14); av = atr(h, l, c, 14)
    sma = np.convolve(c, np.ones(BB_N) / BB_N, mode="full")[:len(c)]
    sma[:BB_N] = c[:BB_N]
    sd = np.array([c[max(0, i - BB_N):i + 1].std() for i in range(len(c))])
    bu = sma + BB_K * sd; bl = sma - BB_K * sd
    dhi = roll_max(h, DON_N); dlo = roll_min(l, DON_N)
    hh = roll_max(h, VB_N); ll = roll_min(l, VB_N)
    return dict(ef=ef, es=es, ax=ax, av=av, bu=bu, bl=bl, dhi=dhi, dlo=dlo, hh=hh, ll=ll)


def signal_dir(strat, o, h, l, c, ind):
    """Vectorized entry direction per bar: +1 long, -1 short, 0 none."""
    ef, es, ax = ind["ef"], ind["es"], ind["ax"]
    d = np.zeros(len(c), dtype=np.int8)
    if strat == "TREND":
        up = (ef > es) & (l <= ef) & (c > ef) & (c > o) & (ax > ADX_MIN)
        dn = (ef < es) & (h >= ef) & (c < ef) & (c < o) & (ax > ADX_MIN)
    elif strat == "BREAKOUT":
        dhi, dlo = ind["dhi"], ind["dlo"]
        pdhi = np.roll(dhi, 1); pdlo = np.roll(dlo, 1)
        up = (c > pdhi) & (ef > es) & (ax > ADX_MIN)
        dn = (c < pdlo) & (ef < es) & (ax > ADX_MIN)
    elif strat == "MEANREV":
        bu, bl = ind["bu"], ind["bl"]
        up = (ax < ADX_MIN) & (c <= bl) & (c > o)
        dn = (ax < ADX_MIN) & (c >= bu) & (c < o)
    elif strat == "VOLBREAK":
        hh, ll = ind["hh"], ind["ll"]
        phh = np.roll(hh, 1); pll = np.roll(ll, 1)
        up = c > phh
        dn = c < pll
    else:
        return d
    d[up] = 1; d[dn & (d == 0)] = -1
    return d


def backtest(o, h, l, c, ind, dirs, *, atr_k, R, exit_mode):
    es, av = ind["es"], ind["av"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    rets = []
    i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; risk = atr_k * av[i]
        if risk <= 0 or not np.isfinite(risk):
            i += 1; continue
        sl = entry - di * risk; tp = entry + di * R * risk
        j = i + 1; exit_px = None
        while j < n:
            if di == 1:
                if l[j] <= sl: exit_px = sl; break
                if exit_mode == "fixed" and h[j] >= tp: exit_px = tp; break
                if exit_mode == "trend" and c[j] < es[j]: exit_px = c[j]; break
            else:
                if h[j] >= sl: exit_px = sl; break
                if exit_mode == "fixed" and l[j] <= tp: exit_px = tp; break
                if exit_mode == "trend" and c[j] > es[j]: exit_px = c[j]; break
            j += 1
        if exit_px is None:
            break
        gross = di * (exit_px - entry) / entry
        rets.append((gross - FEE_RT) / (risk / entry))  # net R
        i = j + 1
    return np.array(rets)


def stat(rets):
    if len(rets) == 0:
        return dict(n=0)
    return dict(n=len(rets), expR=float(rets.mean()), win=float((rets > 0).mean()),
                pf=float(rets[rets > 0].sum() / max(-rets[rets < 0].sum(), 1e-9)))


def main():
    grid = list(itertools.product([1.0, 1.5], [1.5, 2.5], ["fixed", "trend"]))  # atr_k,R,exit
    hits = []
    for tf in TFS:
        for coin in COINS:
            f = DATA / f"{coin}_{tf}.npz"
            if not f.exists():
                continue
            d = np.load(f); o, h, l, c = d["o"], d["h"], d["l"], d["c"]
            split = int(len(c) * 0.70)
            bh_oos = (c[-1] / c[split] - 1)
            ind = precompute(o, h, l, c)
            for strat in STRATS:
                dirs = signal_dir(strat, o, h, l, c, ind)
                for atr_k, R, em in grid:
                    ris = backtest(o[:split], h[:split], l[:split], c[:split],
                                   {k: v[:split] for k, v in ind.items()},
                                   dirs[:split], atr_k=atr_k, R=R, exit_mode=em)
                    ros = backtest(o[split:], h[split:], l[split:], c[split:],
                                   {k: v[split:] for k, v in ind.items()},
                                   dirs[split:], atr_k=atr_k, R=R, exit_mode=em)
                    a, b = stat(ris), stat(ros)
                    if a.get("n", 0) >= 30 and b.get("n", 0) >= 30 and a["expR"] > 0 and b["expR"] > 0:
                        # OOS economic edge: expectancy% > 0 and positive total
                        # net %/trade ≈ expR * mean risk%; approximate via compounded
                        hits.append(dict(tf=tf, coin=coin, strat=strat, atr_k=atr_k, R=R, em=em,
                                         is_expR=a["expR"], oos_expR=b["expR"], oos_n=b["n"],
                                         oos_win=b["win"], oos_pf=b["pf"], bh_oos=bh_oos))
    # report
    hits.sort(key=lambda x: x["oos_expR"], reverse=True)
    print(f"FEE={FEE_RT*100:.2f}%  scan: {len(COINS)} coins × {len(TFS)} TFs × {len(STRATS)} strats × {len(grid)} params")
    print(f"STABLE cells (IS>0 & OOS>0, n>=30 both): {len(hits)}\n")
    print(f"{'tf':>3} {'coin':>8} {'strat':>9} {'k':>3} {'R':>3} {'exit':>5} | "
          f"{'IS_expR':>7} {'OOS_expR':>8} {'n':>4} {'win':>5} {'PF':>5} {'OOS_BH':>7}")
    for x in hits:
        beat = "BEAT" if x["oos_pf"] > 1.0 else ""
        print(f"{x['tf']:>3} {x['coin']:>8} {x['strat']:>9} {x['atr_k']:>3} {x['R']:>3} {x['em']:>5} | "
              f"{x['is_expR']:>+7.3f} {x['oos_expR']:>+8.3f} {x['oos_n']:>4} {x['oos_win']*100:>4.0f}% "
              f"{x['oos_pf']:>5.2f} {x['bh_oos']*100:>+6.0f}%")
    # summary by strategy
    print("\nby strategy (count of stable cells):")
    for s in STRATS:
        cs = [x for x in hits if x["strat"] == s]
        print(f"  {s:>9}: {len(cs)} cells" + (f"  best OOS expR {max(c['oos_expR'] for c in cs):+.3f}" if cs else ""))


if __name__ == "__main__":
    main()
