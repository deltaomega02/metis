"""Gate-sensitivity sweep — is ema50>200 too slow? Would a faster filter do better?

Same long-only breakout strategy + risk 0.75% + cap4. Only the BTC regime GATE
changes, from none → fast → slow:
  none        : no regime filter (every long breakout)
  px>ema200   : price above the 200-EMA (fast, flips early)
  ema20>50    : fast moving-average cross
  ema50>200   : the current gate (slow golden cross)
Reports per gate: trade count, OOS expR, portfolio total/CAGR/MDD/Sharpe, win%.
The question: does a faster gate catch the turn earlier for MORE money, or just
add false-signal trades that bleed (lower expR, deeper MDD)?
"""
from __future__ import annotations

import numpy as np

from risk_sweep import collect, port_sim, DATA
from backtest_trend_15m import ema

RISK = 0.0075


def gate_array(kind):
    b = np.load(DATA / "BTCUSDT_4h.npz"); bt = b["t"].astype(np.int64); c = b["c"]
    if kind == "none":      arr = np.ones(len(c), dtype=bool)
    elif kind == "px>ema200": arr = c > ema(c, 200)
    elif kind == "ema20>50":  arr = ema(c, 20) > ema(c, 50)
    elif kind == "ema50>200": arr = ema(c, 50) > ema(c, 200)
    else: raise ValueError(kind)
    return bt, arr


def up_at(bt, arr, ts):
    i = int(np.searchsorted(bt, ts, side="right")) - 1
    return bool(arr[i]) if i >= 0 else False


def main():
    coins = sorted({f.name[:-7] for f in DATA.glob("*_4h.npz")})
    raw = collect(coins)
    ts_all = sorted(ti for ti, _, _, _ in raw)
    split = ts_all[int(len(ts_all) * 0.70)]
    print(f"게이트 민감도 · long-only 돌파 · risk{RISK*100:.2f}% · cap4 · {len(coins)}코인 4h\n")
    print(f"{'게이트':>11} | {'n':>5} {'OOS expR':>9} {'win%':>5} | {'total':>8} {'CAGR':>7} {'MDD':>5} {'Sharpe':>7}")
    for kind in ("none", "px>ema200", "ema20>50", "ema50>200"):
        bt, arr = gate_array(kind)
        lg = [(ti, to, r, sp) for (ti, to, r, sp) in raw if up_at(bt, arr, ti)]
        if len(lg) < 30:
            print(f"{kind:>11} | n={len(lg)} 적음"); continue
        R = np.array([r for _, _, r, _ in lg])
        osR = np.array([r for ti, _, r, _ in lg if ti >= split])
        m = port_sim(lg, RISK)
        print(f"{kind:>11} | {len(lg):>5} {osR.mean():>+9.3f} {(R>0).mean()*100:>4.0f}% | "
              f"{m['total']*100:>+7.0f}% {m['cagr']*100:>+6.1f}% {m['mdd']*100:>4.0f}% {m['sharpe']:>7.2f}")
    print("\n→ 빠른 게이트가 total↑ & MDD 안 나빠지면 = 네 말대로 약하게 가는 게 맞음.")
    print("  빠른 게이트가 expR↓·MDD↑면 = 가짜신호 비용이 커서 느린 게이트가 정당.")


if __name__ == "__main__":
    main()
