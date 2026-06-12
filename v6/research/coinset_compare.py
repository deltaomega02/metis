"""Coin-universe comparison for the live strategy (long-only + BTC gate, risk 0.75%).

Same engine as risk_sweep; only the symbol universe changes. Reports per set the
full-history portfolio path AND the out-of-sample (last 30%) expR, so we pick the
universe by realized growth + OOS robustness, not by in-sample curve-fit. More
coins = more breakout candidates to fill the 4 slots in a bull, but noisier alts
can dilute quality — this measures which way it nets out.
"""
from __future__ import annotations

import numpy as np

from risk_sweep import btc_gate, collect, port_sim, up_at, GATE

CURRENT8 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
MAJ12 = CURRENT8 + ["LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT"]
LIQ16 = MAJ12 + ["AVAXUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT"]
ALL23 = ["AAVEUSDT", "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT", "BNBUSDT",
         "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETHUSDT", "FILUSDT", "INJUSDT", "LINKUSDT",
         "LTCUSDT", "NEARUSDT", "OPUSDT", "SOLUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT",
         "UNIUSDT", "XRPUSDT"]

SETS = [("current8", CURRENT8), ("major12", MAJ12), ("liquid16", sorted(set(LIQ16))), ("all23", ALL23)]
RISK = 0.0075


def main():
    bt, arr = btc_gate(GATE)
    print(f"코인셋 비교 · long-only + BTC {GATE} gate · risk {RISK*100:.2f}% · cap4 · 4h")
    print(f"{'set':>9} | {'코인':>4} {'n':>5} {'expR':>7} {'OOSexpR':>8} {'win':>5} | "
          f"{'total':>8} {'CAGR':>7} {'MDD':>5} {'Sharpe':>7} {'스킵':>6}")
    for name, coins in SETS:
        raw = collect(coins)
        lg = [(ti, to, r, sp) for (ti, to, r, sp) in raw if up_at(bt, arr, ti)]
        if len(lg) < 30:
            print(f"{name:>9} | n={len(lg)} 적음"); continue
        R = np.array([r for _, _, r, _ in lg])
        ts = sorted(ti for ti, _, _, _ in lg); split = ts[int(len(ts) * 0.70)]
        osR = np.array([r for ti, _, r, _ in lg if ti >= split])
        m = port_sim(lg, RISK)
        print(f"{name:>9} | {len(coins):>4} {len(lg):>5} {R.mean():>+7.3f} "
              f"{osR.mean():>+8.3f} {(R>0).mean()*100:>4.0f}% | "
              f"{m['total']*100:>+7.0f}% {m['cagr']*100:>+6.1f}% {m['mdd']*100:>4.0f}% "
              f"{m['sharpe']:>7.2f} {m['skipped']:>6}")


if __name__ == "__main__":
    main()
