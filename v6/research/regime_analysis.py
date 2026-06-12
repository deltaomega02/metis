"""When/why did the breakout edge weaken? Quarter-by-quarter breakdown.

Pools all 23-coin BREAKOUT trades, buckets by calendar quarter, and shows expR
with long/short split. Each quarter is tagged with BTC's regime (up/down/chop)
so we can see whether weakness tracks bear/choppy markets — testing the
"recent markets are crazy (Trump/tariffs/war), all chop+bear" hypothesis.

"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backtest_trend_15m import FEE_RT
from edge_scan import precompute, signal_dir

DATA = Path(__file__).resolve().parent / "data"


def bt_side(o, h, l, c, t, ind, dirs, atr_k=1.5, slip=2, fund=0.0001, tfh=4):
    cost = FEE_RT + 2 * slip / 10000.0
    es, av = ind["es"], ind["av"]
    n = len(c); warm = 60
    out = []; i = warm
    while i < n - 1:
        di = dirs[i]
        if di == 0:
            i += 1; continue
        entry = c[i]; risk = atr_k * av[i]
        if risk <= 0 or not np.isfinite(risk):
            i += 1; continue
        sl = entry - di * risk; j = i + 1; expx = None
        while j < n:
            if di == 1:
                if l[j] <= sl: expx = sl; break
                if c[j] < es[j]: expx = c[j]; break
            else:
                if h[j] >= sl: expx = sl; break
                if c[j] > es[j]: expx = c[j]; break
            j += 1
        if expx is None:
            break
        net = di * (expx - entry) / entry - cost - (j - i) * (tfh / 8) * fund
        out.append((int(t[i]), net / (risk / entry), "L" if di == 1 else "S"))
        i = j + 1
    return out


def quarter(ms):
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def main():
    allt = []
    for f in sorted(DATA.glob("*_4h.npz")):
        d = np.load(f)
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        allt += bt_side(o, h, l, c, t, ind, dirs)

    # BTC quarterly return → regime
    b = np.load(DATA / "BTCUSDT_4h.npz")
    bq = defaultdict(list)
    for ms, cl in zip(b["t"], b["c"]):
        bq[quarter(int(ms))].append(float(cl))
    btc_ret = {q: (v[-1] / v[0] - 1) * 100 for q, v in bq.items()}

    byq = defaultdict(list)
    for ms, r, s in allt:
        byq[quarter(ms)].append((r, s))

    print(f"total trades={len(allt)}  (23 coins, BREAKOUT 4h)\n")
    print(f"{'quarter':>8} {'BTC':>6} {'regime':>6} | {'n':>4} {'expR':>7} | {'L_n':>4} {'L_expR':>7} | {'S_n':>4} {'S_expR':>7}")
    for q in sorted(byq):
        rows = byq[q]
        rs = np.array([r for r, _ in rows])
        L = np.array([r for r, s in rows if s == "L"])
        S = np.array([r for r, s in rows if s == "S"])
        ret = btc_ret.get(q, 0)
        reg = "up" if ret > 10 else "down" if ret < -10 else "chop"
        print(f"{q:>8} {ret:>+5.0f}% {reg:>6} | {len(rs):>4} {rs.mean():>+7.3f} | "
              f"{len(L):>4} {(L.mean() if len(L) else 0):>+7.3f} | {len(S):>4} {(S.mean() if len(S) else 0):>+7.3f}")

    # aggregate by regime
    print("\n── regime별 집계 ──")
    reg_bucket = defaultdict(list)
    for q in byq:
        ret = btc_ret.get(q, 0)
        reg = "up" if ret > 10 else "down" if ret < -10 else "chop"
        reg_bucket[reg] += [r for r, _ in byq[q]]
    for reg in ("up", "down", "chop"):
        a = np.array(reg_bucket[reg]) if reg_bucket[reg] else np.array([0.0])
        print(f"  {reg:>5}: n={len(reg_bucket[reg]):>4}  expR={a.mean():+.3f}  win={(a>0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
