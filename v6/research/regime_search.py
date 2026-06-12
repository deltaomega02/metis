"""Search realtime BTC-regime detectors that align trade direction with the
market, validated walk-forward. Rule: take a coin's breakout only if its
direction matches BTC's regime (long iff BTC up, short iff BTC down) — judged
realtime (only bars up to entry). Compares many detectors; robust = positive
in MOST folds AND overall, not curve-fit to one window.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import ema
from edge_scan import precompute, signal_dir
from regime_analysis import bt_side

DATA = Path(__file__).resolve().parent / "data"


def shift_gt(c, n):
    """c[i] > c[i-n] (momentum up), realtime."""
    out = np.zeros(len(c), dtype=bool)
    out[n:] = c[n:] > c[:-n]
    return out


def donch_state(c, n):
    """+1 once price makes n-bar high, -1 on n-bar low, holds otherwise → up bool."""
    up = np.zeros(len(c), dtype=bool)
    state = True
    for i in range(len(c)):
        if i >= n:
            hi = c[i - n:i].max(); lo = c[i - n:i].min()
            if c[i] > hi: state = True
            elif c[i] < lo: state = False
        up[i] = state
    return up


def main():
    b = np.load(DATA / "BTCUSDT_4h.npz")
    bt = b["t"].astype(np.int64); bc = b["c"]
    regimes = {
        "ema50>200": ema(bc, 50) > ema(bc, 200),
        "ema20>50": ema(bc, 20) > ema(bc, 50),
        "ema20>100": ema(bc, 20) > ema(bc, 100),
        "px>ema100": bc > ema(bc, 100),
        "px>ema200": bc > ema(bc, 200),
        "mom~5d(30b)": shift_gt(bc, 30),
        "mom~20d(120b)": shift_gt(bc, 120),
        "donch50_state": donch_state(bc, 50),
    }

    def reg_at(ts, arr):
        i = int(np.searchsorted(bt, ts, side="right")) - 1
        return bool(arr[i]) if i >= 0 else True

    tr = []
    for f in sorted(DATA.glob("*_4h.npz")):
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tr += bt_side(o, h, l, c, t, ind, dirs)

    ts_all = sorted(x[0] for x in tr)
    edges = np.linspace(ts_all[0], ts_all[-1] + 1, 6)  # 5 folds

    def evaluate(filt):
        R = np.array([r for ti, r, s in tr if filt(ti, s)])
        if len(R) < 20:
            return None
        folds = []
        for k in range(5):
            seg = [r for ti, r, s in tr if filt(ti, s) and edges[k] <= ti < edges[k + 1]]
            folds.append(np.mean(seg) if len(seg) >= 5 else None)
        pos = sum(1 for f in folds if f is not None and f > 0)
        return len(R), float(R.mean()), float(R.sum()), folds, pos

    print(f"trades={len(tr)}  walk-forward 5 folds · rule: signal-dir == BTC regime\n")
    print(f"{'detector':>16} | {'n':>5} {'expR':>7} {'총R':>7} {'folds+':>6}  fold expR")
    # baseline: no regime filter (current v6 = take all)
    base = evaluate(lambda ti, s: True)
    print(f"{'(없음=현v6)':>16} | {base[0]:>5} {base[1]:>+7.3f} {base[2]:>+7.0f} {base[4]:>5}/5  " +
          " ".join(f"{x:+.2f}" if x is not None else "  - " for x in base[3]))
    for name, arr in regimes.items():
        m = evaluate(lambda ti, s, a=arr: (s == "L") == reg_at(ti, a))
        if m:
            print(f"{name:>16} | {m[0]:>5} {m[1]:>+7.3f} {m[2]:>+7.0f} {m[4]:>5}/5  " +
                  " ".join(f"{x:+.2f}" if x is not None else "  - " for x in m[3]))

    # best detector → long/short split
    print("\n── 참고: 각 detector의 롱/숏 분리 expR (regime 정렬 적용) ──")
    for name, arr in regimes.items():
        L = np.array([r for ti, r, s in tr if s == "L" and reg_at(ti, arr)])
        S = np.array([r for ti, r, s in tr if s == "S" and not reg_at(ti, arr)])
        ls = f"롱 n={len(L)} {L.mean():+.3f}" if len(L) else "롱 -"
        ss = f"숏 n={len(S)} {S.mean():+.3f}" if len(S) else "숏 -"
        print(f"  {name:>16} | {ls:<22} | {ss}")


if __name__ == "__main__":
    main()
