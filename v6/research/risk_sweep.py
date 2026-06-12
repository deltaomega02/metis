"""Risk-per-trade sweep for the LIVE strategy (long-only + BTC uptrend gate).

For risk ∈ {0.5%, 0.75%, 1.0%} with the 4-concurrent cap:
  - portfolio equity path → total return / CAGR / MDD / Sharpe
  - Monte-Carlo bootstrap (5000) → P(final<0), MDD p50/p95
  - LIQUIDATION check: stop-distance %% distribution vs the leverage-3 liquidation
    band (~32% adverse). If every stop sits far inside the band, liquidation cannot
    precede the stop — risk%% does NOT change this (it scales qty, not the %% move
    to liquidation; only leverage does).
  - MARGIN feasibility: worst-case simultaneous margin / equity at the 4-cap, so we
    know whether 1%% would ever starve a new entry (the live balance-guard then skips).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import FEE_RT, ema
from edge_scan import precompute, signal_dir

DATA = Path(__file__).resolve().parent / "data"
GATE = "ema50_200"      # the deployed BTC uptrend gate
LEV = 3                  # deployed leverage
MMR = 0.005              # maintenance margin ~0.5%
CAP = 4


def btc_gate(kind):
    b = np.load(DATA / "BTCUSDT_4h.npz"); bt = b["t"].astype(np.int64); bc = b["c"]
    if kind == "ema50_200": arr = ema(bc, 50) > ema(bc, 200)
    elif kind == "ema20_50": arr = ema(bc, 20) > ema(bc, 50)
    else: arr = bc > ema(bc, 200)
    return bt, arr


def up_at(bt, arr, ts):
    i = int(np.searchsorted(bt, ts, side="right")) - 1
    return bool(arr[i]) if i >= 0 else False


def collect(coins, atr_k=1.5, slip=2, fund=0.0001, tfh=4):
    """Long-only breakout; returns (ti, to, R, stop_pct) per trade."""
    cost = FEE_RT + 2 * slip / 10000.0
    out = []
    for coin in coins:
        f = DATA / f"{coin}_4h.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        es, av = ind["es"], ind["av"]
        n = len(c); i = 60
        while i < n - 1:
            if dirs[i] != 1:   # long-only
                i += 1; continue
            entry = c[i]; risk = atr_k * av[i]
            if risk <= 0 or not np.isfinite(risk):
                i += 1; continue
            sl = entry - risk; j = i + 1; expx = None
            while j < n:
                if l[j] <= sl: expx = sl; break
                if c[j] < es[j]: expx = c[j]; break
                j += 1
            if expx is None:
                break
            net = (expx - entry) / entry - cost - (j - i) * (tfh / 8) * fund
            out.append((int(t[i]), int(t[j]), net / (risk / entry), risk / entry))
            i = j + 1
    return out


def port_sim(tr, risk, cap=CAP):
    """Equity path with concurrent cap + margin guard (skip entry if margin short)."""
    ev = []
    for k, (ti, to, r, sp) in enumerate(tr):
        ev.append((ti, 1, k)); ev.append((to, 0, k))
    ev.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0; mdd = 0.0; openc = 0; used_margin = 0.0
    taken = {}; R = []; skipped = 0; max_margin_frac = 0.0
    spd = {k: tr[k] for k in range(len(tr))}
    for ts, typ, k in ev:
        ti, to, r, sp = spd[k]
        if typ == 1:
            notional = (eq * risk) / sp            # qty*entry in equity units
            margin = notional / LEV
            if openc < cap and used_margin + margin <= eq:
                taken[k] = margin; openc += 1; used_margin += margin
                max_margin_frac = max(max_margin_frac, used_margin / eq)
            else:
                skipped += 1
        elif k in taken:
            eq *= (1 + risk * r); openc -= 1; used_margin -= taken.pop(k); R.append(r)
            peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    days = (max(x[1] for x in tr) - min(x[0] for x in tr)) / 1000 / 86400
    cagr = (eq ** (365 / days) - 1) if days > 30 and eq > 0 else float("nan")
    Ra = np.array(R)
    sh = float((risk * Ra).mean() / (risk * Ra).std() * np.sqrt(len(Ra) / (days / 365))) \
        if len(Ra) > 1 and Ra.std() > 0 else 0.0
    return dict(n=len(R), total=eq - 1, cagr=cagr, mdd=mdd, sharpe=sh,
                skipped=skipped, max_margin=max_margin_frac)


def main():
    np.random.seed(7)
    bt, arr = btc_gate(GATE)
    coins = sorted({f.name[:-7] for f in DATA.glob("*_4h.npz")})
    raw = collect(coins)
    lg = [(ti, to, r, sp) for (ti, to, r, sp) in raw if up_at(bt, arr, ti)]
    R = np.array([r for _, _, r, _ in lg])
    stop_pcts = np.array([sp for _, _, _, sp in lg])

    liq_band = 1.0 / LEV - MMR
    print("=" * 66)
    print(f"RISK SWEEP · long-only + BTC {GATE} gate · lev{LEV} · {len(coins)}coins 4h")
    print(f"n={len(R)} trades  base OOS-equiv expR={R.mean():+.3f}  win={(R>0).mean()*100:.0f}%")
    print("=" * 66)

    # liquidation feasibility
    print(f"\n[청산 점검] 레버{LEV} 청산밴드 ≈ {liq_band*100:.0f}% 역행")
    print(f"  손절거리%: 평균 {stop_pcts.mean()*100:.2f}%  중앙 {np.median(stop_pcts)*100:.2f}%  "
          f"최대 {stop_pcts.max()*100:.2f}%  99%tile {np.percentile(stop_pcts,99)*100:.2f}%")
    over = (stop_pcts >= liq_band).sum()
    print(f"  손절이 청산밴드 이상인 트레이드: {over}건  → "
          + ("청산 위험 있음!" if over else "손절이 항상 청산보다 한참 앞 → 청산 사실상 불가"))
    print("  (리스크%는 청산밴드를 못 바꾼다 — 레버만 바꿈. 0.5/0.75/1.0% 모두 동일하게 청산 무관)")

    # risk sweep
    print(f"\n[리스크% 스윕]  cap={CAP}, 마진가드 포함")
    print(f"{'risk':>6} | {'total':>8} {'CAGR':>7} {'MDD':>6} {'Sharpe':>7} | {'스킵(마진)':>10} {'최대동시마진':>10}")
    for risk in (0.005, 0.0075, 0.01):
        m = port_sim(lg, risk)
        print(f"{risk*100:>5.2f}% | {m['total']*100:>+7.0f}% {m['cagr']*100:>+6.1f}% "
              f"{m['mdd']*100:>5.0f}% {m['sharpe']:>7.2f} | {m['skipped']:>10} {m['max_margin']*100:>9.0f}%")

    # monte carlo per risk
    print(f"\n[몬테카를로 5000회]  파산확률 / 최종손익 / MDD")
    for risk in (0.005, 0.0075, 0.01):
        finals, mdds = [], []
        for _ in range(5000):
            s = np.random.choice(R, len(R), replace=True)
            eq = np.cumprod(1 + risk * s); pk = np.maximum.accumulate(eq)
            finals.append(eq[-1] - 1); mdds.append(((pk - eq) / pk).max())
        finals, mdds = np.array(finals), np.array(mdds)
        print(f"  {risk*100:.2f}%: P(최종<0)={ (finals<0).mean()*100:>4.1f}%  "
              f"P(반토막↓)={ (finals<-0.5).mean()*100:>4.1f}%  "
              f"final p5={np.percentile(finals,5)*100:+.0f}% p50={np.percentile(finals,50)*100:+.0f}% "
              f"p95={np.percentile(finals,95)*100:+.0f}%  MDD p95={np.percentile(mdds,95)*100:.0f}%")


if __name__ == "__main__":
    main()
