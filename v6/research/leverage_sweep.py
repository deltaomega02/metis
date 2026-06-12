"""Leverage sweep — does leverage change returns? (Spoiler: no, by design.)

Same live strategy (long-only + BTC gate, risk 0.75%, cap 4). Position size is
risk-based (qty = equity·risk ÷ stop_distance), so LEVERAGE DOES NOT ENTER THE
SIZE — every leverage trades the identical position and books the identical P&L.
Leverage only changes (a) margin locked per position → whether the 4-cap can all
open in a drawdown (margin guard), and (b) the liquidation band. This sweep makes
that explicit and flags the leverage at which the deepest stop would sit OUTSIDE
the liquidation band (= liquidation could fire before the stop = real danger).
"""
from __future__ import annotations

import numpy as np

from risk_sweep import btc_gate, collect, up_at, GATE, DATA

RISK = 0.0075
CAP = 4
MMR = 0.005


def port_sim(tr, lev, risk=RISK, cap=CAP):
    ev = []
    for k, (ti, to, r, sp) in enumerate(tr):
        ev.append((ti, 1, k)); ev.append((to, 0, k))
    ev.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0; mdd = 0.0; openc = 0; used = 0.0
    taken = {}; R = []; skipped = 0; maxm = 0.0
    spd = {k: tr[k] for k in range(len(tr))}
    for ts, typ, k in ev:
        ti, to, r, sp = spd[k]
        if typ == 1:
            margin = (eq * risk / sp) / lev
            if openc < cap and used + margin <= eq:
                taken[k] = margin; openc += 1; used += margin
                maxm = max(maxm, used / eq)
            else:
                skipped += 1
        elif k in taken:
            eq *= (1 + risk * r); openc -= 1; used -= taken.pop(k); R.append(r)
            peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    days = (max(x[1] for x in tr) - min(x[0] for x in tr)) / 1000 / 86400
    cagr = (eq ** (365 / days) - 1) if days > 30 and eq > 0 else float("nan")
    Ra = np.array(R)
    sh = float((risk * Ra).mean() / (risk * Ra).std() * np.sqrt(len(Ra) / (days / 365))) \
        if len(Ra) > 1 and Ra.std() > 0 else 0.0
    return dict(total=eq - 1, cagr=cagr, mdd=mdd, sharpe=sh, skipped=skipped, maxm=maxm)


def main():
    bt, arr = btc_gate(GATE)
    coins = sorted({f.name[:-7] for f in DATA.glob("*_4h.npz")})
    raw = collect(coins)
    lg = [(ti, to, r, sp) for (ti, to, r, sp) in raw if up_at(bt, arr, ti)]
    stop = np.array([sp for _, _, _, sp in lg])
    print(f"long-only+BTC {GATE} gate · risk {RISK*100:.2f}% · cap{CAP} · {len(lg)} trades")
    print(f"손절거리%: 평균 {stop.mean()*100:.2f}%  최대 {stop.max()*100:.2f}%  99%tile {np.percentile(stop,99)*100:.2f}%\n")
    print(f"{'레버':>4} | {'total':>8} {'CAGR':>7} {'MDD':>5} {'Sharpe':>7} | {'마진스킵':>7} {'최대동시마진':>10} | {'청산밴드':>7} {'손절>밴드':>9}")
    for lev in (3, 5, 7, 10):
        m = port_sim(lg, lev)
        band = 1.0 / lev - MMR
        over = int((stop >= band).sum())
        flag = "⚠위험" if over else "안전"
        print(f"{lev:>3}x | {m['total']*100:>+7.0f}% {m['cagr']*100:>+6.1f}% {m['mdd']*100:>4.0f}% "
              f"{m['sharpe']:>7.2f} | {m['skipped']:>7} {m['maxm']*100:>9.0f}% | "
              f"{band*100:>6.1f}% {str(over)+'건 '+flag:>9}")
    print("\n→ total/CAGR/MDD/Sharpe가 레버와 무관하게 (거의) 동일하면 = 레버는 수익을 안 바꾼다는 증거.")
    print("  '손절>밴드'가 0건이어야 안전(손절이 청산보다 먼저). 1건이라도 있으면 그 레버는 청산 위험.")


if __name__ == "__main__":
    main()
