"""v6 final gate — does the BREAKOUT edge survive friction, walk-forward, and an
8-coin portfolio with realistic risk sizing and a concurrent-position cap?

  FRICTION    taker 0.11% RT + slippage (bps/side) + funding (0.01%/8h on hold).
  PORTFOLIO   event-driven: enter only while concurrent positions < cap; each
              open trade risks `risk_frac` of equity; PnL booked at close (R).
  WALK-FWD    5 sequential folds — the edge must hold in most, not one window.

Strategy fixed from the scan: BREAKOUT (Donchian-20 break in EMA-trend dir,
ADX>22), structural SL = k·ATR, exit = ride until close crosses slow EMA.

"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir, COINS, EMA_S, DON_N, BB_N, VB_N
from backtest_trend_15m import FEE_RT

DATA = Path(__file__).resolve().parent / "data"
TF_HOURS = {"4h": 4, "1h": 1}


def backtest_fr(o, h, l, c, t, ind, dirs, *, atr_k, R, exit_mode,
                slip_bps, fund_rate, tf_hours):
    """One coin's BREAKOUT trades with friction. Returns (t_in, t_out, R_net)."""
    cost = FEE_RT + 2 * slip_bps / 10000.0          # round-trip taker + slippage
    es, av = ind["es"], ind["av"]
    n = len(c); warm = max(EMA_S, DON_N, BB_N, VB_N) + 5
    trades = []
    i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; risk = atr_k * av[i]
        if risk <= 0 or not np.isfinite(risk):
            i += 1; continue
        sl = entry - di * risk; tp = entry + di * R * risk
        j = i + 1; expx = None
        while j < n:
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if exit_mode == "fixed" and h[j] >= tp: expx = tp; break
                if exit_mode == "trend" and c[j] < es[j]: expx = c[j]; break
            else:
                if h[j] >= sl: expx = sl; break
                if exit_mode == "fixed" and l[j] <= tp: expx = tp; break
                if exit_mode == "trend" and c[j] > es[j]: expx = c[j]; break
            j += 1
        if expx is None:
            break
        gross = di * (expx - entry) / entry
        net = gross - cost - (j - i) * (tf_hours / 8.0) * fund_rate
        trades.append((int(t[i]), int(t[j]), net / (risk / entry)))
        i = j + 1
    return trades


def portfolio_sim(trades, *, risk_frac, cap):
    """Event-driven equity. Enter only while open positions < cap; book at close."""
    events = []
    for k, (ti, to, R) in enumerate(trades):
        events.append((ti, 1, k, R))   # open
        events.append((to, 0, k, R))   # close (typ 0 sorts before opens at same ts)
    events.sort(key=lambda x: (x[0], x[1]))
    eq = peak = 1.0; mdd = 0.0; openc = 0; taken = set(); realized = []
    for ts, typ, k, R in events:
        if typ == 1:
            if openc < cap:
                taken.add(k); openc += 1
        elif k in taken:
            eq *= (1 + risk_frac * R); openc -= 1; realized.append(R)
            peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    if len(realized) < 10:
        return None
    Rs = np.array(realized)
    days = (max(x[1] for x in trades) - min(x[0] for x in trades)) / 1000 / 86400
    gp = Rs[Rs > 0].sum(); gl = -Rs[Rs < 0].sum()
    return dict(n=len(realized), skipped=len(trades) - len(realized),
                win=float((Rs > 0).mean()), pf=float(gp / max(gl, 1e-9)),
                total=float(eq - 1), mdd=mdd,
                cagr=(eq ** (365.0 / days) - 1) if days > 30 and eq > 0 else float("nan"))


def collect(tf, *, atr_k, R, exit_mode, slip_bps, fund_rate):
    """All-coin BREAKOUT trades at this friction."""
    out = []
    for coin in COINS:
        f = DATA / f"{coin}_{tf}.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        out += backtest_fr(o, h, l, c, t, ind, dirs, atr_k=atr_k, R=R, exit_mode=exit_mode,
                           slip_bps=slip_bps, fund_rate=fund_rate, tf_hours=TF_HOURS[tf])
    return out


def main():
    atr_k, R, exit_mode, fund, slip, tf = 1.5, 2.5, "trend", 0.0001, 2, "4h"
    print(f"BREAKOUT 4h  atr_k={atr_k} R={R} exit={exit_mode} slip={slip}bp "
          f"funding={fund*100:.3f}%/8h\n")
    trades = collect(tf, atr_k=atr_k, R=R, exit_mode=exit_mode, slip_bps=slip, fund_rate=fund)
    print(f"total signals (no cap): {len(trades)}")

    print("\n-- risk/trade × concurrent-cap sweep --")
    print(f"  {'risk':>5} {'cap':>4} | {'taken':>5} {'skip':>5} {'win':>5} {'PF':>5} "
          f"{'total':>8} {'CAGR':>7} {'MDD':>6}")
    for rf in (0.003, 0.005, 0.01):
        for cap in (3, 4, 99):
            m = portfolio_sim(trades, risk_frac=rf, cap=cap)
            if m:
                capn = "none" if cap == 99 else str(cap)
                print(f"  {rf*100:>4.1f}% {capn:>4} | {m['n']:>5} {m['skipped']:>5} "
                      f"{m['win']*100:>4.0f}% {m['pf']:>5.2f} {m['total']*100:>+7.0f}% "
                      f"{m['cagr']*100:>+6.1f}% {m['mdd']*100:>5.1f}%")

    print("\n-- walk-forward 5 folds (raw R, cap-agnostic sign check) --")
    tr = sorted(trades); edges = np.linspace(tr[0][0], tr[-1][0] + 1, 6)
    for k in range(5):
        seg = [x for x in tr if edges[k] <= x[0] < edges[k + 1]]
        if len(seg) >= 10:
            Rs = np.array([x[2] for x in seg])
            gp = Rs[Rs > 0].sum(); gl = -Rs[Rs < 0].sum()
            d0 = datetime.fromtimestamp(edges[k] / 1000, tz=timezone.utc)
            print(f"  fold{k+1} {d0:%Y-%m}: n={len(seg):>4} expR={Rs.mean():>+.3f} "
                  f"PF={gp/max(gl,1e-9):>.2f} win={(Rs>0).mean()*100:>3.0f}%")

    print("\n-- per-coin (raw R, 2bp) --")
    for coin in COINS:
        f = DATA / f"{coin}_{tf}.npz"
        if not f.exists():
            continue
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        ind = precompute(o, h, l, c)
        ctr = backtest_fr(o, h, l, c, t, ind, signal_dir("BREAKOUT", o, h, l, c, ind),
                          atr_k=atr_k, R=R, exit_mode=exit_mode, slip_bps=slip,
                          fund_rate=fund, tf_hours=TF_HOURS[tf])
        if len(ctr) >= 10:
            Rs = np.array([x[2] for x in ctr])
            gp = Rs[Rs > 0].sum(); gl = -Rs[Rs < 0].sum()
            print(f"  {coin:>8}: n={len(ctr):>4} expR={Rs.mean():>+.3f} PF={gp/max(gl,1e-9):>.2f}")


if __name__ == "__main__":
    main()
