"""Realistic backtest v2 — the deployed 4h+1D ls portfolio with real-world frictions.

Upgrades over the standard sim (exit_redesign/daily_ls):
  1. INTRABAR RESOLUTION: every short exit bar where BOTH the stop and the 2.5R
     target sit inside the bar's range is resolved with Bybit 1m klines (which
     level was crossed first). The standard sim assumes SL-first (pessimistic).
  2. STOP/TP SLIPPAGE: for every SL/TP exit, the fill is re-priced from the 1m
     bar that first crossed the trigger — fill = worse(trigger, that 1m close).
     Replaces the flat 2bp exit-slip assumption with measured movement.
  3. REAL FUNDING: per-trade funding cost summed from Bybit's actual 8h funding
     history over the holding window (sign-aware: longs pay positive rates,
     shorts receive). Replaces the flat 0.01%/8h assumption.
Entry slippage stays 2bp (market order, $100-150 notional at top-of-book).
1m windows are fetched ONLY for exit bars (+ ambiguity bars) — ~1k calls, cached.

Output: per-trade R_v2 stream → block-bootstrap 1-year probability table,
side-by-side with the v1 assumptions, plus a decomposition of the difference.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import numpy as np

from backtest_trend_15m import FEE_RT
from edge_scan import precompute, signal_dir

DATA = Path(__file__).resolve().parent / "data"
CACHE = Path(__file__).resolve().parent / "data_realistic"
CACHE.mkdir(exist_ok=True)
MAJ = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
       "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
API = "https://api.bybit.com"
ENTRY_SLIP = 2 / 10000.0


# ───────────────────────── data plumbing ─────────────────────────
def resample_d(o, h, l, c, t, f=6):
    n = (len(c) // f) * f
    return (o[:n].reshape(-1, f)[:, 0], h[:n].reshape(-1, f).max(1),
            l[:n].reshape(-1, f).min(1), c[:n].reshape(-1, f)[:, -1],
            t[:n].reshape(-1, f)[:, 0])


def http_get(client, path, params, tries=5):
    for a in range(tries):
        try:
            r = client.get(API + path, params=params, timeout=15)
            d = r.json()
            if str(d.get("retCode")) == "0":
                return d["result"]
        except Exception:
            pass
        time.sleep(0.5 * (a + 1))
    return None


def fetch_funding(client, sym):
    """Full 8h funding-rate history → sorted [(ts_ms, rate)] (cached)."""
    f = CACHE / f"funding_{sym}.json"
    if f.exists():
        return json.loads(f.read_text())
    out, end = [], int(time.time() * 1000)
    while True:
        res = http_get(client, "/v5/market/funding/history",
                       {"category": "linear", "symbol": sym, "limit": 200, "endTime": end})
        rows = (res or {}).get("list", [])
        if not rows:
            break
        out += [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in rows]
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest >= end or len(rows) < 200:
            break
        end = oldest - 1
        time.sleep(0.15)
    out = sorted(set(out))
    f.write_text(json.dumps(out))
    return out


def fetch_1m_window(client, sym, start_ms, minutes):
    """1m klines covering [start_ms, start_ms+minutes) → ascending rows (cached)."""
    f = CACHE / f"m1_{sym}_{start_ms}_{minutes}.json"
    if f.exists():
        return json.loads(f.read_text())
    rows, cur = [], start_ms
    end_ms = start_ms + minutes * 60_000
    while cur < end_ms:
        res = http_get(client, "/v5/market/kline",
                       {"category": "linear", "symbol": sym, "interval": "1",
                        "start": cur, "end": end_ms - 1, "limit": 1000})
        lst = (res or {}).get("list", [])
        if not lst:
            break
        lst = sorted(lst, key=lambda r: int(r[0]))
        rows += lst
        nxt = int(lst[-1][0]) + 60_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    rows = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows]
    f.write_text(json.dumps(rows))
    time.sleep(0.12)
    return rows


# ───────────────────────── trade walk with metadata ─────────────────────────
def walk_trades(tf_h):
    """Deployed mixed-exit walk, returning rich metadata per trade."""
    out = []
    for coin in MAJ:
        d = np.load(DATA / f"{coin}_4h.npz")
        o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
        if tf_h == 24:
            o, h, l, c, t = resample_d(o, h, l, c, t)
        ind = precompute(o, h, l, c)
        dirs = signal_dir("BREAKOUT", o, h, l, c, ind)
        es, av = ind["es"], ind["av"]
        n = len(c)
        i = 75
        while i < n - 1:
            di = dirs[i]
            if di == 0:
                i += 1; continue
            entry = c[i]; rk = 1.5 * av[i]
            if rk <= 0 or not np.isfinite(rk):
                i += 1; continue
            sl = entry - di * rk
            tp = entry - 2.5 * rk if di == -1 else 0.0   # short fixed 2.5R only
            j = i + 1; reason = None; expx = None
            while j < n:
                if di == 1:
                    if l[j] <= sl: reason, expx = "SL", sl; break
                    if c[j] < es[j]: reason, expx = "TREND", c[j]; break
                else:
                    hit_sl = h[j] >= sl
                    hit_tp = l[j] <= tp
                    if hit_sl and hit_tp: reason, expx = "AMBIG", sl; break  # v1=SL 가정(비관)
                    if hit_sl: reason, expx = "SL", sl; break
                    if hit_tp: reason, expx = "TP", tp; break
                j += 1
            if reason is None:
                break
            out.append(dict(coin=coin, arm=("1d" if tf_h == 24 else "4h"), dir=di,
                            ti=int(t[i]), to=int(t[j]), entry=float(entry),
                            sl=float(sl), tp=float(tp), rk=float(rk),
                            exit_bar_start=int(t[j]), reason=reason, expx=float(expx),
                            hold_h=(j - i) * tf_h))
            i = j + 1
    return out


def apply_portfolio(t4, t1, cap4=4, cap1=1):
    """Exclusivity + per-arm caps → mark taken trades (deployed semantics)."""
    ev = []
    for tr in t4 + t1:
        ev.append((tr["ti"], 1, tr)); ev.append((tr["to"], 0, tr))
    ev.sort(key=lambda x: (x[0], x[1]))
    oc = {"4h": 0, "1d": 0}; held = {}; taken = []
    for ts, typ, tr in ev:
        cap = cap4 if tr["arm"] == "4h" else cap1
        if typ == 1:
            if oc[tr["arm"]] >= cap or tr["coin"] in held or tr.get("_in"):
                continue
            tr["_in"] = True; oc[tr["arm"]] += 1; held[tr["coin"]] = tr["arm"]
        elif tr.get("_in") and not tr.get("_done"):
            tr["_done"] = True; oc[tr["arm"]] -= 1; taken.append(tr)
            if held.get(tr["coin"]) == tr["arm"]:
                del held[tr["coin"]]
    return taken


# ───────────────────────── realistic refinement ─────────────────────────
def refine(trades, client):
    bar_min = {"4h": 240, "1d": 1440}
    n_amb = n_amb_flip = n_slip = miss = 0
    slip_bps_all = []
    for k, tr in enumerate(trades):
        if k % 100 == 0:
            print(f"  refine {k}/{len(trades)}  (1m cache hit/fetch)", flush=True)
        mins = bar_min[tr["arm"]]
        # ① 모호 봉(숏 SL·TP 동시터치) → 1m로 선후 판정
        if tr["reason"] == "AMBIG":
            n_amb += 1
            rows = fetch_1m_window(client, tr["coin"], tr["exit_bar_start"], mins)
            first = "SL"
            for r in rows:
                if r[2] >= tr["sl"]: first = "SL"; break
                if r[3] <= tr["tp"]: first = "TP"; break
            if first == "TP":
                n_amb_flip += 1
            tr["reason"] = first
            tr["expx"] = tr["sl"] if first == "SL" else tr["tp"]
        # ② SL/TP 체결 슬리피지: 트리거 교차 1m봉의 종가로 재가격(불리한 쪽만)
        if tr["reason"] in ("SL", "TP"):
            rows = fetch_1m_window(client, tr["coin"], tr["exit_bar_start"], mins)
            trig = tr["expx"]; di = tr["dir"]
            crossed = None
            for r in rows:
                if tr["reason"] == "SL":
                    hit = (di == 1 and r[3] <= trig) or (di == -1 and r[2] >= trig)
                else:  # short TP
                    hit = r[3] <= trig
                if hit:
                    crossed = r; break
            if crossed is None:
                miss += 1  # 1m 데이터 없음(상장 초기 등) → v1 가정 유지
            else:
                cl = crossed[4]
                # 손절: 롱은 아래로, 숏은 위로 밀린 만큼 / 익절(숏): 아래로 통과분은 유리하나
                # 시장가 특성상 트리거보다 유리하게 잡진 않음 → worse-of 적용
                if tr["reason"] == "SL":
                    fill = min(trig, cl) if di == 1 else max(trig, cl)
                else:
                    fill = max(trig, cl)          # 숏 TP: 트리거 아래로 더 가도 트리거 체결로
                slip = abs(fill - trig) / trig
                slip_bps_all.append(slip * 1e4)
                tr["expx"] = fill
                n_slip += 1
    print(f"  모호봉 {n_amb}건 중 TP 선행으로 뒤집힘 {n_amb_flip}건 · 슬리피지 측정 {n_slip}건 "
          f"(중앙값 {np.median(slip_bps_all) if slip_bps_all else 0:.1f}bp, "
          f"p90 {np.percentile(slip_bps_all,90) if slip_bps_all else 0:.1f}bp) · 1m없음 {miss}건")
    return trades


def apply_funding(trades, fund_map):
    for tr in trades:
        rates = fund_map[tr["coin"]]
        ts = np.array([x[0] for x in rates]); rs = np.array([x[1] for x in rates])
        m = (ts > tr["ti"]) & (ts <= tr["to"])
        # rate>0 → 롱이 지불/숏이 수취. cost_frac>0 = 비용.
        tr["fund_frac"] = float(rs[m].sum()) * (1 if tr["dir"] == 1 else -1)
    return trades


def r_net(tr, real=True):
    di = tr["dir"]
    e_fill = tr["entry"] * (1 + di * ENTRY_SLIP)
    gross = di * (tr["expx"] - e_fill) / tr["rk"]
    fee = FEE_RT * tr["entry"] / tr["rk"]
    if real:
        fund = tr.get("fund_frac", 0.0) * tr["entry"] / tr["rk"]
    else:  # v1 가정: 0.01%/8h + 출구 2bp
        fund = 0.0001 * (tr["hold_h"] / 8) * tr["entry"] / tr["rk"]
        gross -= ENTRY_SLIP * tr["entry"] / tr["rk"]
    return gross - fee - fund


def bootstrap(Rs, per_year, risk=0.0075, B=10000, blk=25, seed=42):
    rng = np.random.default_rng(seed)
    fin = np.empty(B); mdd = np.empty(B)
    for b in range(B):
        out = np.empty(per_year); i = 0
        while i < per_year:
            s = rng.integers(0, max(1, len(Rs) - blk))
            take = min(blk, per_year - i); out[i:i + take] = Rs[s:s + take]; i += take
        eq = np.cumprod(1 + risk * out)
        peak = np.maximum.accumulate(eq)
        fin[b] = eq[-1]; mdd[b] = ((peak - eq) / peak).max()
    ret = fin - 1
    return dict(p_pos=float((ret > 0).mean()), p20=float((ret > .2).mean()),
                p50=float((ret > .5).mean()), pneg10=float((ret < -.1).mean()),
                med=float(np.percentile(ret, 50)), p5=float(np.percentile(ret, 5)),
                p95=float(np.percentile(ret, 95)),
                mdd20=float((mdd > .2).mean()), mdd30=float((mdd > .3).mean()),
                mdd_med=float(np.percentile(mdd, 50)))


def main():
    print("① 거래 walk (4h+1D, 배포 시맨틱)...", flush=True)
    t4 = walk_trades(4); t1 = walk_trades(24)
    taken = apply_portfolio(t4, t1)
    taken.sort(key=lambda x: x["to"])
    yrs = (taken[-1]["to"] - taken[0]["ti"]) / 1000 / 86400 / 365.25
    per_year = int(len(taken) / yrs)
    n_exit = sum(1 for t in taken if t["reason"] in ("SL", "TP", "AMBIG"))
    print(f"  체결 {len(taken)}건/{yrs:.1f}y (연 {per_year}) · 1m 필요 출구 {n_exit}건", flush=True)

    with httpx.Client() as client:
        print("② 실제 펀딩 히스토리 (8코인 전 기간)...", flush=True)
        fund_map = {s: fetch_funding(client, s) for s in MAJ}
        for s in MAJ:
            print(f"  {s}: {len(fund_map[s])} rates", flush=True)
        print("③ 1m 정밀화 (모호봉 판정 + 스탑/TP 슬리피지 실측)...", flush=True)
        taken = refine(taken, client)
    taken = apply_funding(taken, fund_map)

    R1 = np.array([r_net(t, real=False) for t in taken])
    R2 = np.array([r_net(t, real=True) for t in taken])
    print(f"\navgR: v1(가정) {R1.mean():+.3f} → v2(실측) {R2.mean():+.3f}  (Δ {R2.mean()-R1.mean():+.4f})")
    fund_R = np.array([t.get("fund_frac", 0) * t["entry"] / t["rk"] for t in taken])
    print(f"  실펀딩 영향: 평균 {-fund_R.mean():+.4f}R/trade (양수=우리에게 유리)")

    b1 = bootstrap(R1, per_year); b2 = bootstrap(R2, per_year)
    print(f"\n=== 1년 확률표: v1(가정) vs v2(리얼리스틱) ===")
    rows = [("P(+수익)", "p_pos", "%"), ("P(>+20%)", "p20", "%"), ("P(>+50%)", "p50", "%"),
            ("P(<-10%)", "pneg10", "%"), ("중앙값", "med", "ret"), ("p5", "p5", "ret"),
            ("p95", "p95", "ret"), ("P(MDD>20%)", "mdd20", "%"), ("P(MDD>30%)", "mdd30", "%"),
            ("MDD중앙값", "mdd_med", "ret")]
    for label, k, kind in rows:
        f = (lambda v: f"{v*100:5.1f}%")
        print(f"  {label:>10}: v1 {f(b1[k])}   v2 {f(b2[k])}")

    np.save(CACHE / "R2_stream.npy", R2)
    print("\nR2 스트림 저장 → data_realistic/R2_stream.npy")


if __name__ == "__main__":
    main()
