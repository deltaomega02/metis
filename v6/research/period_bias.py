"""Is long's edge just the 2020-21/2023-24 bull markets? Slice long-vs-short by
the BTC quarterly regime the trade fell in, and re-check with the big bull
quarters removed. (Quarter regime is hindsight — fine for a period-bias autopsy,
not a live gate.)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from edge_scan import precompute, signal_dir
from regime_analysis import bt_side

DATA = Path(__file__).resolve().parent / "data"


def q(ms):
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def main():
    b = np.load(DATA / "BTCUSDT_4h.npz")
    bq = defaultdict(list)
    for ms, cl in zip(b["t"], b["c"]):
        bq[q(int(ms))].append(float(cl))
    btc_ret = {k: (v[-1] / v[0] - 1) * 100 for k, v in bq.items()}

    tr = []
    for f in sorted(DATA.glob("*_4h.npz")):
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tr += bt_side(o, h, l, c, t, ind, dirs)

    def slice(qfilt):
        L = np.array([r for ti, r, s in tr if s == "L" and qfilt(btc_ret.get(q(ti), 0))])
        S = np.array([r for ti, r, s in tr if s == "S" and qfilt(btc_ret.get(q(ti), 0))])
        return L, S

    def line(name, qfilt):
        L, S = slice(qfilt)
        def f(a): return f"n={len(a):>4} expR={a.mean():>+.3f} 총R={a.sum():>+6.1f}" if len(a) else "n=0"
        print(f"  {name:>22} | 롱: {f(L):<34} | 숏: {f(S)}")

    print("기간/regime별 롱 vs 숏 (BTC 분기수익 기준)\n")
    line("전체", lambda x: True)
    line("극단상승(>+30%) 제외", lambda x: x <= 30)
    line("상승장(>+10%)", lambda x: x > 10)
    line("평상/횡보(-10~+10%)", lambda x: -10 <= x <= 10)
    line("하락장(<-10%)", lambda x: x < -10)
    line("강한하락(<-25%)", lambda x: x < -25)


if __name__ == "__main__":
    main()
