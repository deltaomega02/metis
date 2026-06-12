"""Clear-the-confusion summary: trade COUNT vs total PROFIT, and where shorts
actually make/lose money. Total R = sum of per-trade R (proxy for cumulative
profit); expR = average per trade. Fewer trades with +expR can beat many with
-expR. BTC trend judged realtime (EMA50>200, no look-ahead).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from backtest_trend_15m import ema
from edge_scan import precompute, signal_dir
from regime_analysis import bt_side

DATA = Path(__file__).resolve().parent / "data"


def main():
    b = np.load(DATA / "BTCUSDT_4h.npz")
    bt_t = b["t"].astype(np.int64)
    btc_up = ema(b["c"], 50) > ema(b["c"], 200)

    def up_at(ts):
        i = int(np.searchsorted(bt_t, ts, side="right")) - 1
        return i >= 0 and bool(btc_up[i])

    tr = []
    for f in sorted(DATA.glob("*_4h.npz")):
        d = np.load(f); o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if len(c) < 260:
            continue
        ind = precompute(o, h, l, c); dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        tr += bt_side(o, h, l, c, t, ind, dirs)

    def show(name, filt):
        a = np.array([r for ti, r, s in tr if filt(ti, s)])
        if len(a) == 0:
            print(f"  {name:>22}: 없음"); return
        print(f"  {name:>22}: n={len(a):>4}  평균expR={a.mean():>+.3f}  총R(누적)={a.sum():>+7.1f}  win={(a>0).mean()*100:.0f}%")

    print("=== 전체(23코인) 방향별 ===")
    show("롱 전체", lambda ti, s: s == "L")
    show("숏 전체", lambda ti, s: s == "S")
    print("\n=== 숏을 쪼개보면 (어디서 먹나) ===")
    show("숏 · BTC상승장", lambda ti, s: s == "S" and up_at(ti))
    show("숏 · BTC하락장", lambda ti, s: s == "S" and not up_at(ti))
    print("\n=== 롱을 쪼개보면 ===")
    show("롱 · BTC상승장", lambda ti, s: s == "L" and up_at(ti))
    show("롱 · BTC하락장", lambda ti, s: s == "L" and not up_at(ti))
    print("\n=== 운영 후보 비교 (거래수 vs 총수익) ===")
    show("A) 다 거래(현 v6)", lambda ti, s: True)
    show("B) BTC상승+롱만", lambda ti, s: s == "L" and up_at(ti))
    show("C) BTC상승(롱+숏)", lambda ti, s: up_at(ti))
    show("D) 롱 전체(게이트X)", lambda ti, s: s == "L")


if __name__ == "__main__":
    main()
