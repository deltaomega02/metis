#!/usr/bin/env python3
"""METIS dashboard — zero-dependency monochrome status page (bare http.server).

PAPER mode renders an A/B/C comparison of the gate arms (none / ema20>50 /
ema50>200): each arm is an independent paper portfolio (its own state DB), and the
top table compares equity, return, drawdown, trade stats and gate state so we can
see which gate actually compounds best. LIVE mode shows the single live arm.
Shared below: per-coin market state (same indicators the engine uses), combined
open positions and recent trades. No auto-refresh; mark prices fetched on load.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import (BYBIT, MARKET_SNAPSHOT_PATH, PAPER_ARMS, PAPER_INITIAL_BALANCE_USDT,
                             PAPER_MODE, RISK, STATE_DB_PATH, TRADING, arm_db_path)
import sqlite3
import time

from core.market_snapshot import read_snapshot


# ─────────────── helpers ───────────────
def get_usdkrw() -> float:
    try:
        with urlopen("https://api.exchangerate-api.com/v4/latest/USD", timeout=3) as r:
            return float(json.loads(r.read())["rates"]["KRW"])
    except Exception:
        return 1370.0


def q(sql: str, params: tuple = (), path=STATE_DB_PATH) -> list[dict]:
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def is_active(svc: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                              text=True, timeout=2).stdout.strip() == "active"
    except Exception:
        return False


def free_mem_mi() -> int:
    try:
        for line in subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=2).stdout.splitlines():
            if line.startswith("Mem:"):
                return int(line.split()[2])
    except Exception:
        pass
    return 0


def disk_pct() -> str:
    try:
        return subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=2).stdout.splitlines()[-1].split()[3]
    except Exception:
        return "?"


def esc(s) -> str:
    return html_lib.escape(str(s)) if s is not None else ""


def _spark(points: list, seed: float, w: int = 320, h: int = 44) -> str:
    """Inline SVG sparkline of the mark-to-market equity curve, with a dashed
    seed baseline. Green if the last point is above seed, red below."""
    if not points or len(points) < 2:
        return '<span class="muted" style="font-size:11px">곡선 축적 중 — 사이클마다 1점</span>'
    lo = min(min(points), seed); hi = max(max(points), seed); rng = (hi - lo) or 1.0
    n = len(points)
    pts = " ".join(f"{i / (n - 1) * w:.1f},{h - (p - lo) / rng * h:.1f}" for i, p in enumerate(points))
    y0 = h - (seed - lo) / rng * h
    col = "#059669" if points[-1] >= seed else "#dc2626"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<line x1="0" y1="{y0:.1f}" x2="{w}" y2="{y0:.1f}" stroke="#cbd5e1" stroke-width="1" stroke-dasharray="3 3"/>'
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


# Line styles for the overlay chart — monochrome base, the most-active arm darkest/thickest.
_ARM_STYLE = {"none": ("#94a3b8", 1.4, ""), "fast": ("#475569", 1.6, "5 3"),
              "slow": ("#94a3b8", 1.6, "1 3"), "ls": ("#94a3b8", 1.6, "4 3"),
              "live": ("#0f172a", 2.4, "")}


def _multi_chart(arms: list, w: int = 1080, h: int = 240) -> str:
    """One big overlay of every arm's mark-to-market curve on a shared scale, with a
    dashed seed baseline and left-edge $ ticks. Arms align on their cycle timestamps."""
    allts = sorted({t for a in arms for t, _ in a.get("curve", [])})
    if len(allts) < 2:
        return '<div class="muted">곡선 축적 중 — 4h 사이클마다 1점씩 쌓입니다</div>'
    xi = {t: i for i, t in enumerate(allts)}
    seed = arms[0]["seed"] if arms else 1000.0
    vals = [m for a in arms for _, m in a.get("curve", [])] + [seed]
    lo, hi = min(vals), max(vals); rng = (hi - lo) or 1.0
    PADL, PADR, PADT, PADB = 52, 14, 14, 8
    W = w - PADL - PADR; H = h - PADT - PADB
    def X(t): return PADL + xi[t] / (len(allts) - 1) * W
    def Y(v): return PADT + H - (v - lo) / rng * H
    s = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="max-width:100%;height:auto">']
    # horizontal gridlines + $ ticks
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = lo + rng * frac; y = Y(v)
        s.append(f'<line x1="{PADL}" y1="{y:.1f}" x2="{w - PADR}" y2="{y:.1f}" stroke="#f1f0eb" stroke-width="1"/>')
        s.append(f'<text x="{PADL - 6}" y="{y + 3:.0f}" font-size="10" fill="#9ca3af" text-anchor="end">${v:,.0f}</text>')
    # seed baseline
    ys = Y(seed)
    s.append(f'<line x1="{PADL}" y1="{ys:.1f}" x2="{w - PADR}" y2="{ys:.1f}" stroke="#cbd5e1" stroke-width="1.2" stroke-dasharray="4 3"/>')
    s.append(f'<text x="{w - PADR}" y="{ys - 4:.0f}" font-size="9.5" fill="#cbd5e1" text-anchor="end">시드 ${seed:,.0f}</text>')
    legend = []
    for a in arms:
        key = a["label"].split()[0]
        col, sw, dash = _ARM_STYLE.get(a.get("name_key", key), ("#475569", 1.5, ""))
        cur = a.get("curve", [])
        if len(cur) >= 2:
            pts = " ".join(f"{X(t):.1f},{Y(m):.1f}" for t, m in cur)
            da = f' stroke-dasharray="{dash}"' if dash else ""
            s.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="{sw}"{da} '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
            t, m = cur[-1]
            s.append(f'<circle cx="{X(t):.1f}" cy="{Y(m):.1f}" r="3" fill="{col}"/>')
        dsh = "border-bottom:2px dashed" if dash else "border-bottom:2px solid"
        legend.append(f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px">'
                      f'<span style="width:16px;{dsh} {col}"></span>{esc(key)} '
                      f'<b style="color:{col}">${a["mark_equity"]:,.0f}</b></span>')
    s.append('</svg>')
    return "".join(s) + f'<div style="font-size:11.5px;margin-top:8px;color:#4b5563">{"".join(legend)}</div>'


_TICK = {"ts": 0.0, "v": {}}


def live_marks() -> dict:
    """Current price per traded symbol via ONE tickers request (all linear symbols
    in a single call — negligible rate-limit weight, unlike the per-coin klines),
    cached 15s. Returns {} on failure so the caller falls back to the snapshot price.
    This is the only live exchange call the dashboard makes: cheap and bounded."""
    now = time.time()
    if _TICK["v"] and now - _TICK["ts"] < 15:
        return _TICK["v"]
    try:
        with urlopen(f"{BYBIT.base_url}/v5/market/tickers?category=linear", timeout=4) as r:
            lst = json.loads(r.read()).get("result", {}).get("list", [])
        v = {x["symbol"]: float(x["lastPrice"]) for x in lst
             if x.get("symbol") in TRADING.SYMBOLS and x.get("lastPrice")}
        if v:
            _TICK.update(ts=now, v=v)
            return v
    except Exception:
        pass
    return _TICK["v"]


def coin_status(m: dict, held_side: str | None) -> tuple[str, str]:
    """Operator-facing status line for a coin from its snapshot indicators and
    whether an arm holds it. Pure formatting — no exchange calls."""
    if held_side:
        return ("보유 중 (롱)" if held_side == "Buy" else "보유 중 (숏)", "hold")
    if not m.get("trending"):
        return (f"무추세 관망 (ADX {m['adx']:.0f})", "mut")
    if m.get("long_cand"):
        return ("🟢 상승 돌파 — 롱 진입", "pos")
    if m.get("short_cand"):
        return ("🔴 하락 돌파 — 숏 진입", "neg")
    if m.get("up"):
        return (f"상승추세 · 롱돌파까지 +{m['dist_hi']:.1f}%", "mut")
    return (f"하락추세 · 숏돌파까지 -{m.get('dist_lo', 0):.1f}%", "mut")


def _cost_bp(side: str, ref: float, fill: float, is_exit: bool = False) -> float | None:
    """체결 비용 bp (양수=불리). 진입: Buy는 비싸게 사면 +, Sell은 싸게 팔면 +.
    청산은 방향 반전(숏 청산=매수)."""
    if not ref or not fill:
        return None
    d = 1 if side == "Buy" else -1
    if is_exit:
        d = -d
    return d * (fill - ref) / ref * 1e4


def _experiment(live: dict, paper: dict) -> dict:
    """실전 vs 페이퍼 검증 실험 지표 — 같은 신호 페어 매칭 + 비용 실측 통계.
    페어 = 심볼·방향 동일 + 진입시각 15분 내(버퍼 45s vs 150s 어긋남 흡수)."""
    from datetime import datetime

    def ts(s):
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    pairs = []
    for st, lrows, prows in (("보유", live["positions"], paper["positions"]),
                             ("청산", live["trades"], paper["trades"])):
        for lp in lrows:
            for pp in prows:
                if (lp["symbol"] == pp["symbol"] and lp["side"] == pp["side"]
                        and abs(ts(lp["opened_utc"]) - ts(pp["opened_utc"])) < 900):
                    ref = lp.get("ref_entry") or 0
                    pairs.append(dict(
                        sym=lp["symbol"], side=lp["side"], st=st,
                        t=(lp["opened_utc"] or "")[5:16], ref=ref,
                        lf=lp["entry_price"], pf=pp["entry_price"],
                        lbp=_cost_bp(lp["side"], ref, lp["entry_price"]),
                        pbp=_cost_bp(pp["side"], pp.get("ref_entry") or ref, pp["entry_price"]),
                        lR=lp.get("realized_R"), pR=pp.get("realized_R")))
                    break
    # 진입 비용: 라이브 전 체결(보유+청산) ref 대비
    ent = [_cost_bp(x["side"], x.get("ref_entry") or 0, x["entry_price"])
           for x in list(live["positions"]) + list(live["trades"])]
    ent = [e for e in ent if e is not None]
    # 청산 비용: 라이브 청산거래 ref_exit 대비
    exi = [_cost_bp(t["side"], t.get("ref_exit") or 0, t.get("exit_price") or 0, is_exit=True)
           for t in live["trades"]]
    exi = [e for e in exi if e is not None]
    closed = live["trades"]
    return dict(pairs=pairs, n_ent=len(ent),
                ent_avg=(sum(ent) / len(ent)) if ent else None,
                n_exi=len(exi), exi_avg=(sum(exi) / len(exi)) if exi else None,
                n_closed=len(closed),
                net=sum((t.get("realized_usdt") or 0) for t in closed))


def _side_html(v) -> str:
    """보유 방향 배지: 롱(초록)/숏(빨강)/미보유(–). v=(side, is_1d) — 1D 암 보유는 ·1D 표기."""
    if not v:
        return '<span class="mut">–</span>'
    side, is_1d = v
    txt = ("롱" if side == "Buy" else "숏") + ("·1D" if is_1d else "")
    cls = "pos" if side == "Buy" else "neg"
    return f'<span class="{cls}">{txt}</span>'


# ─────────────── gather ───────────────
def _arm_summary(label: str, gate_kind: str, path, gates: dict, marks: dict) -> dict:
    eqr = q("SELECT equity_usdt, high_water_usdt, initial_usdt FROM equity WHERE key='main'", path=path)
    eq = eqr[0]["equity_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    hw = eqr[0]["high_water_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    seed = (eqr[0]["initial_usdt"] if eqr and eqr[0].get("initial_usdt") else None) or PAPER_INITIAL_BALANCE_USDT
    positions = q("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_utc", path=path)
    agg = q("""SELECT COUNT(*) n, SUM(CASE WHEN realized_usdt>0 THEN 1 ELSE 0 END) wins,
               AVG(realized_R) avg_r, SUM(realized_usdt) sum_pnl,
               SUM(CASE WHEN realized_usdt>0 THEN realized_usdt ELSE 0 END) gw,
               SUM(CASE WHEN realized_usdt<=0 THEN realized_usdt ELSE 0 END) gl
               FROM trades""", path=path)
    trades = q("SELECT * FROM trades ORDER BY id DESC LIMIT 500", path=path)
    # mark-to-market: realized equity + open-position unrealized at snapshot price
    upnl = 0.0
    for pos in positions:
        mk = marks.get(pos["symbol"], 0.0); dd = 1 if pos["side"] == "Buy" else -1
        if mk > 0:
            upnl += dd * (mk - pos["entry_price"]) * pos["qty"]
    mark_eq = eq + upnl
    try:
        curve = [(r["ts_utc"], r["mark_usdt"]) for r in
                 q("SELECT ts_utc,mark_usdt FROM equity_curve ORDER BY ts_utc", path=path)]
    except Exception:
        curve = []
    return {"label": label, "gate_kind": gate_kind, "equity": eq, "high_water": hw, "seed": seed,
            "pnl_usd": eq - seed, "pnl_pct": (eq - seed) / seed * 100 if seed else 0,
            "unrealized": upnl, "mark_equity": mark_eq,
            "mark_pnl_pct": (mark_eq - seed) / seed * 100 if seed else 0,
            "dd_pct": (hw - eq) / hw * 100 if hw > 0 else 0.0,
            "gate": gates.get(gate_kind, False), "positions": positions, "curve": curve,
            "trades": trades, "agg": agg[0] if agg else {}}


def gather() -> dict:
    d: dict = {}
    d["mode"] = "PAPER" if PAPER_MODE else "LIVE"
    # everything market-related comes from the engine's snapshot file — zero Bybit calls
    snap = read_snapshot(MARKET_SNAPSHOT_PATH) or {}
    d["gates"] = snap.get("gates") or {"none": True, "ema20_50": False, "ema50_200": False}
    d["snap_updated"] = snap.get("updated_utc")
    coins = snap.get("coins") or {}
    snap_prices = {sym: c["price"] for sym, c in coins.items()}
    lm = live_marks()  # one cheap tickers call; {} on failure
    marks = {**snap_prices, **lm}  # live price wins; snapshot price is the fallback
    d["marks"] = marks
    d["marks_live"] = bool(lm)
    if PAPER_MODE:
        d["arms"] = []
        for c in PAPER_ARMS:
            s = _arm_summary(c.label, c.gate, arm_db_path(c.name), d["gates"], marks)
            s["name_key"] = c.name
            d["arms"].append(s)
    else:
        # LIVE: 실전은 지갑이 하나 = **단일 LIVE 뷰로 병합**(4h+1D 포지션·거래 합산,
        # equity는 state.db=지갑 단일 출처 — 두 컬럼이면 같은 돈이 두 번 보임, 운영자 지적).
        # 어떤 암의 거래인지는 거래내역/보유의 ·1D 태그로만 구분. 페이퍼 둘은 진짜
        # 별도 장부(다른 자본 경로)라 분리 유지.
        arms_l = []
        live = _arm_summary("실전 (LIVE)", "none", STATE_DB_PATH, d["gates"], marks)
        live["name_key"] = "live"
        for x in live["positions"]: x["_arm1d"] = False
        for x in live["trades"]: x["_arm1d"] = False
        if arm_db_path("live1d").exists():
            l1 = _arm_summary("", "none", arm_db_path("live1d"), d["gates"], marks)
            for x in l1["positions"]: x["_arm1d"] = True
            for x in l1["trades"]: x["_arm1d"] = True
            live["positions"] = live["positions"] + l1["positions"]
            live["trades"] = sorted(live["trades"] + l1["trades"],
                                    key=lambda t: t.get("closed_utc") or "", reverse=True)
            trs = live["trades"]  # agg 병합 재계산 (지갑 equity는 state.db 그대로)
            n = len(trs)
            wins = sum(1 for t in trs if (t.get("realized_usdt") or 0) > 0)
            gw = sum((t.get("realized_usdt") or 0) for t in trs if (t.get("realized_usdt") or 0) > 0)
            gl = sum((t.get("realized_usdt") or 0) for t in trs if (t.get("realized_usdt") or 0) <= 0)
            live["agg"] = {"n": n, "wins": wins, "sum_pnl": gw + gl, "gw": gw, "gl": gl,
                           "avg_r": (sum((t.get("realized_R") or 0) for t in trs) / n) if n else None}
            upnl = 0.0  # 미실현도 합산 포지션 기준 재계산
            for pos in live["positions"]:
                mk = marks.get(pos["symbol"], 0.0); dd = 1 if pos["side"] == "Buy" else -1
                if mk > 0:
                    upnl += dd * (mk - pos["entry_price"]) * pos["qty"]
            live["unrealized"] = upnl
            live["mark_equity"] = live["equity"] + upnl
            seed = live["seed"]
            live["mark_pnl_pct"] = (live["mark_equity"] - seed) / seed * 100 if seed else 0
        arms_l.append(live)
        paper = _arm_summary("페이퍼 4h (비교)", "none", arm_db_path("ls"), d["gates"], marks)
        paper["name_key"] = "ls"; arms_l.append(paper)
        if arm_db_path("ls1d").exists():
            a = _arm_summary("페이퍼 1D", "none", arm_db_path("ls1d"), d["gates"], marks)
            a["name_key"] = "ls1d"; arms_l.append(a)
        d["arms"] = arms_l
        d["exp"] = _experiment(live, paper)  # 실전 vs 페이퍼 검증 실험 지표
    held_live, held_paper = {}, {}
    for arm in d["arms"]:
        nk = arm.get("name_key") or ""
        tgt = held_live if nk.startswith("live") else held_paper
        for pos in arm["positions"]:
            # (방향, 1D암 여부) — 라이브는 포지션 태그, 페이퍼는 암 이름으로 판별
            tgt[pos["symbol"]] = (pos["side"], bool(pos.get("_arm1d", nk.endswith("1d"))))
    d["held_live"], d["held_paper"] = held_live, held_paper
    market = []
    for sym in TRADING.SYMBOLS:
        m = coins.get(sym)
        if not m:
            continue
        st, tone = coin_status(m, None)  # 상태=순수 시장신호 / 보유는 별도 실·페 컬럼
        m = dict(m); m["status"] = st; m["tone"] = tone
        m["trend"] = "▲ 상승" if m.get("up") else "▼ 하락"
        market.append(m)
    d["market"] = market
    # 시장 국면 (BTC 기준): ADX<22 횡보 / ema50>200 상승 / 하락은 ADX 강도로 세분
    btc = coins.get("BTCUSDT", {})
    badx = btc.get("adx", 0); uptrend = d["gates"].get("ema50_200")
    if badx < 22:
        d["regime"] = ("⏸ 횡보장", "mut")
    elif uptrend:
        d["regime"] = ("🟢 상승장", "pos")
    elif badx >= 55:
        d["regime"] = (f"🔴 강한 하락장 (ADX {badx:.0f})", "neg")
    elif badx >= 35:
        d["regime"] = (f"🔻 중간 하락장 (ADX {badx:.0f})", "neg")
    else:
        d["regime"] = (f"🔻 약한 하락장 (ADX {badx:.0f})", "neg")
    d["svc"] = {s: is_active(s) for s in ("metis-v6", "metis-v6-dashboard")}
    d["mem_mi"] = free_mem_mi()
    d["disk"] = disk_pct()
    d["usdkrw"] = get_usdkrw()
    d["now_kst"] = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")
    return d


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#faf8f4;color:#1a1d24;font:14px/1.5 -apple-system,'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1380px;margin:0 auto;padding:28px 32px 80px}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:20px 28px;background:#fff;border-radius:12px;box-shadow:0 0 0 1px rgba(15,23,42,.06);margin-bottom:26px;gap:24px}
.brand{display:flex;align-items:center;gap:18px}
.brand-mark{font-size:36px;font-weight:900;letter-spacing:-.04em;color:#0f172a}
.brand-divider{width:1px;align-self:stretch;background:#e5e7eb;margin:4px 0}
.brand-title{font-size:13px;font-weight:700;letter-spacing:.22em;color:#0f172a;font-family:'SF Mono',Menlo,monospace}
.brand-title .mode{color:#9ca3af;margin-left:6px}
.brand-sub{font-size:11px;color:#9ca3af;font-weight:500;font-family:'SF Mono',monospace;margin-top:5px}
.topmeta{display:flex;align-items:center;gap:20px;font-size:11.5px;color:#4b5563;font-family:'SF Mono',Menlo,monospace}
.topmeta .lbl{color:#9ca3af;font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:10px}
.topmeta b{color:#0f172a;font-weight:700}
.svc-dot{display:inline-block;width:8px;height:8px;border-radius:50%}
.svc-on{background:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.2)}
.svc-off{background:#d1d5db}
.sec-h{display:flex;align-items:baseline;gap:12px;margin:30px 0 14px;padding:0 4px}
.sec-h h2{font-size:20px;font-weight:700;letter-spacing:-.015em;color:#0f172a}
.sec-tag{font-size:11px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.card{background:#fff;border-radius:14px;padding:22px 24px;box-shadow:0 1px 3px rgba(0,0,0,.04),0 0 0 1px rgba(15,23,42,.05);margin-bottom:14px}
.pos{color:#059669;font-weight:700}.neg{color:#dc2626;font-weight:700}.mut{color:#6b7280;font-weight:600}
.muted{color:#9ca3af;font-size:13px}
table{width:100%;border-collapse:collapse}
th{font-size:10.5px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:10px 12px;border-bottom:1px solid #f1f0eb;background:#fafaf6}
td{padding:11px 12px;font-size:12.5px;border-bottom:1px solid #f6f5f0}
td.mono{font-family:'SF Mono',Menlo,monospace;font-size:11.5px;color:#6b7280}
.badge-on{padding:4px 10px;font-size:11px;font-weight:700;background:#d1fae5;color:#065f46;border-radius:6px}
.mlbl{display:block;font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:3px}
.mini-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px 18px}
.lead td{font-weight:800;background:#f6fdf9}
footer{margin-top:40px;padding-top:20px;border-top:1px solid #f0eee7;font-size:11.5px;color:#9ca3af;text-align:center}
"""


def render(d: dict) -> str:
    rate = d["usdkrw"]; arms = d["arms"]; gates = d["gates"]; mem = d["mem_mi"]
    # snapshot freshness — the dashboard shows engine data, flag it if the engine stalled
    snap_txt, snap_stale = "데이터 없음 — 엔진 첫 사이클 대기", True
    su = d.get("snap_updated")
    if su:
        try:
            sdt = datetime.fromisoformat(su)
            age = (datetime.now(timezone.utc) - sdt).total_seconds()
            snap_stale = age > 5 * 3600  # one 4h cycle + buffer
            kst = (sdt + timedelta(hours=9)).strftime("%m-%d %H:%M")
            snap_txt = f"{kst} KST 기준" + (" ⚠ 갱신 지연(엔진 점검)" if snap_stale else "")
        except Exception:
            pass
    on, off = '<span class="svc-dot svc-on"></span>', '<span class="svc-dot svc-off"></span>'
    p: list[str] = [f'<!doctype html><html><head><meta charset="utf-8"><title>METIS · {d["mode"]}</title>',
                    f'<style>{CSS}</style></head><body><div class="wrap">']

    # topbar
    p.append(
        '<div class="topbar"><div class="brand"><div class="brand-mark">운영자</div>'
        '<div class="brand-divider"></div><div><div class="brand-title">METIS</div>'
        '<div class="brand-sub">8코인 4h ls(롱+숏) mixed · 실전 LIVE + 페이퍼 비교</div></div></div>'
        '<div class="topmeta">'
        f'<span><span class="lbl">engine</span> {on if d["svc"].get("metis-v6") else off}</span>'
        f'<span><span class="lbl">fx</span> <b>₩{rate:,.0f}</b></span>'
        f'<span><span class="lbl">mem</span> <b>{mem}Mi</b></span>'
        f'<span><span class="lbl">disk</span> <b>{esc(d["disk"])}</b></span>'
        f'<span><span class="lbl">kst</span> <b>{esc(d["now_kst"])}</b></span>'
        '</div></div>')

    # 시장 국면 (regime)
    regime_txt, regime_cls = d.get("regime", ("-", "mut"))
    p.append(
        '<div class="card" style="display:flex;gap:30px;align-items:center;margin-bottom:24px">'
        '<div><span class="mlbl">시장 국면 (BTC)</span>'
        f'<div class="{regime_cls}" style="font-size:22px;font-weight:800;margin-top:4px">{regime_txt}</div>'
        f'<div class="{"neg" if snap_stale else "muted"}" style="font-size:10.5px;margin-top:5px">📡 시장 데이터 {esc(snap_txt)}</div></div>'
        f'<div class="muted" style="margin-left:auto;text-align:right;line-height:1.7">ls(롱+숏) mixed · risk {RISK.RISK_PER_TRADE*100:.2f}%<br>'
        f'동시 {RISK.MAX_CONCURRENT}개 · 레버 {RISK.LEVERAGE}x</div></div>')

    # ── arm comparison (centerpiece) ──
    lead = max(range(len(arms)), key=lambda i: arms[i]["mark_equity"]) if arms else -1
    p.append('<div class="sec-h" style="margin-top:0"><h2>⚔️ 실전 vs 페이퍼</h2>'
             '<span class="sec-tag">같은 신호 · 실전=실체결, 페이퍼=가정 · 슬리피지·성과 비교</span></div>')
    p.append('<div class="card"><table><thead><tr><th>구분</th><th>평가액(M2M)</th><th>수익률</th>'
             '<th>미실현</th><th>MDD</th><th>거래</th><th>승률</th><th>평균R</th><th>PF</th><th>보유</th></tr></thead><tbody>')
    for i, arm in enumerate(arms):
        a = arm["agg"] or {}
        n = a.get("n") or 0; wins = a.get("wins") or 0
        wr = wins / n * 100 if n else 0
        avg_r = a.get("avg_r") or 0
        gw = a.get("gw") or 0; gl = abs(a.get("gl") or 0)
        pf = f"{gw / gl:.2f}" if gl > 0 else ("∞" if gw > 0 else "–")
        mpnl = arm["mark_pnl_pct"]
        pcls = "pos" if mpnl > 0 else ("neg" if mpnl < 0 else "mut")
        u = arm["unrealized"]; ucls = "pos" if u > 0 else ("neg" if u < 0 else "mut")
        lead_cls = ' class="lead"' if i == lead and n + len(arm["positions"]) > 0 else ""
        p.append(f'<tr{lead_cls}><td><b>{esc(arm["label"])}</b></td>'
                 f'<td class="mono">${arm["mark_equity"]:,.0f}</td>'
                 f'<td class="{pcls}">{mpnl:+.2f}%</td>'
                 f'<td class="{ucls}">{("$%+.2f" % u) if arm["positions"] else "–"}</td>'
                 f'<td class="mut">{arm["dd_pct"]:.1f}%</td>'
                 f'<td>{n}</td><td>{wr:.0f}%</td>'
                 f'<td class="{"pos" if avg_r>=0 else "neg"}">{avg_r:+.2f}R</td>'
                 f'<td>{pf}</td><td>{len(arm["positions"])}</td></tr>')
    p.append('</tbody></table>'
             '<div class="muted" style="margin-top:10px">평가액 = 실현 + 미실현(현재가 M2M). '
             '실전(LIVE)·페이퍼가 같은 ls(롱+숏) 돌파 신호를 받음 — 실전은 실제 체결(슬리피지·수수료 반영), '
             '페이퍼는 가정 체결. 둘의 차이 = 실거래 비용. 초록 행 = 선두.</div></div>')

    # ── mark-to-market equity curves ──
    p.append('<div class="sec-h"><h2>📈 평가액 추이</h2>'
             '<span class="sec-tag">M2M(실현+미실현) · 4h 사이클마다 1점 · 점선 = 시드</span></div>')
    p.append('<div class="card">' + _multi_chart(arms) + '</div>')
    crows = []
    for arm in arms:
        spark = _spark([m for _, m in arm["curve"]], arm["seed"])
        u = arm["unrealized"]; ucls = "pos" if u > 0 else ("neg" if u < 0 else "mut")
        mcls = "pos" if arm["mark_equity"] >= arm["seed"] else "neg"
        crows.append(f'<tr><td><b>{esc(arm["label"])}</b></td><td>{spark}</td>'
                     f'<td class="mono {mcls}">${arm["mark_equity"]:,.2f}</td>'
                     f'<td class="mono {ucls}">${u:+.2f}</td></tr>')
    p.append('<div class="card"><table><thead><tr><th>변종</th><th>곡선</th><th>평가액</th>'
             '<th>미실현</th></tr></thead><tbody>' + "".join(crows) + '</tbody></table></div>')

    # ── 🧪 검증 실험: 실전 vs 페이퍼 (같은 신호, 실체결 vs 가정체결) ──
    exp = d.get("exp")
    if exp:
        p.append('<div class="sec-h"><h2>🧪 검증 실험</h2>'
                 '<span class="sec-tag">실전 vs 페이퍼 · 같은 신호 페어 · 비용 양수=불리</span></div>')
        p.append('<div class="card">')
        ea = exp["ent_avg"]; xa = exp["exi_avg"]
        ent_txt = f'{ea:+.1f}bp' if ea is not None else '–'
        ent_cls = "mut" if ea is None else ("pos" if ea <= 2.0 else "neg")
        exi_txt = f'{xa:+.1f}bp' if xa is not None else '대기 (청산 0건)'
        verdict = ('–' if ea is None else
                   ('✓ 가정(+2bp)보다 좋음' if ea < 2.0 else ('≈ 가정 수준' if ea <= 3.0 else '✗ 가정보다 나쁨')))
        p.append('<div class="mini-grid">'
                 f'<div><span class="mlbl">진입비용 실측(n={exp["n_ent"]})</span><b class="{ent_cls}">{ent_txt}</b></div>'
                 f'<div><span class="mlbl">페이퍼 가정</span><b>+2.0bp</b></div>'
                 f'<div><span class="mlbl">판정</span><b>{verdict}</b></div>'
                 f'<div><span class="mlbl">청산비용 실측(n={exp["n_exi"]})</span><b>{exi_txt}</b></div>'
                 f'<div><span class="mlbl">증명 표본(청산)</span><b>{exp["n_closed"]}건 · net ${exp["net"]:+.2f}</b></div>'
                 '</div>')
        if exp["pairs"]:
            p.append('<table style="margin-top:14px"><thead><tr><th>진입(UTC)</th><th>심볼</th><th>방향</th>'
                     '<th>상태</th><th>신호가</th><th>실전체결</th><th>페이퍼체결</th>'
                     '<th>실전bp</th><th>페이퍼bp</th><th>R 실/페</th></tr></thead><tbody>')
            for q_ in exp["pairs"]:
                lb = f'{q_["lbp"]:+.1f}' if q_["lbp"] is not None else '–'
                pb = f'{q_["pbp"]:+.1f}' if q_["pbp"] is not None else '–'
                lcls = "mut" if q_["lbp"] is None else ("pos" if q_["lbp"] <= (q_["pbp"] or 2) else "neg")
                rtxt = (f'{q_["lR"]:+.2f} / {q_["pR"]:+.2f}'
                        if q_["lR"] is not None and q_["pR"] is not None else '–')
                p.append(f'<tr><td class="mono">{esc(q_["t"])}</td><td><b>{esc(q_["sym"])}</b></td>'
                         f'<td>{"롱" if q_["side"]=="Buy" else "숏"}</td><td class="mut">{q_["st"]}</td>'
                         f'<td class="mono">{q_["ref"]:.4f}</td><td class="mono">{q_["lf"]:.4f}</td>'
                         f'<td class="mono">{q_["pf"]:.4f}</td>'
                         f'<td class="{lcls}">{lb}</td><td class="mono mut">{pb}</td>'
                         f'<td class="mono">{rtxt}</td></tr>')
            p.append('</tbody></table>')
        else:
            p.append('<div class="muted" style="margin-top:10px">아직 실전·페이퍼 동시 진입 페어 없음 — 다음 공통 신호부터 자동 누적</div>')
        p.append('</div>')

    # ── combined open positions ──
    marks = d["marks"]
    pos_rows = []
    for arm in arms:
        for pos in arm["positions"]:
            mk = marks.get(pos["symbol"], 0.0); dd = 1 if pos["side"] == "Buy" else -1
            u = dd * (mk - pos["entry_price"]) * pos["qty"] if mk > 0 else 0.0
            risk = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
            rr = u / risk if risk > 0 else 0.0
            ucls = "pos" if u >= 0 else "neg"
            vlabel = arm["label"].split()[0] + ("·1D" if pos.get("_arm1d") else "")
            pos_rows.append(
                f'<tr><td><b>{esc(vlabel)}</b></td><td><b>{esc(pos["symbol"])}</b></td>'
                f'<td>{"롱" if pos["side"]=="Buy" else "숏"} x{pos["leverage"]}</td>'
                f'<td class="mono">{pos["qty"]}</td><td class="mono">{pos["entry_price"]:.4f}</td>'
                f'<td class="mono">{pos["stop_price"]:.4f}</td><td class="mono">{mk:.4f}</td>'
                f'<td class="{ucls}">${u:+.2f}</td><td class="{ucls}">{rr:+.2f}R</td></tr>')
    live_tag = "미실현 실시간 (ticker)" if d.get("marks_live") else "미실현 · 스냅샷 가격(ticker 실패)"
    p.append(f'<div class="sec-h"><h2>🎯 보유 포지션</h2><span class="sec-tag">변종별 · {live_tag}</span></div>')
    if pos_rows:
        p.append('<div class="card"><table><thead><tr><th>변종</th><th>심볼</th><th>방향</th><th>수량</th>'
                 '<th>진입</th><th>SL</th><th>현재가</th><th>미실현$</th><th>R</th></tr></thead><tbody>')
        p.append("".join(pos_rows)); p.append('</tbody></table></div>')
    else:
        p.append('<div class="card"><div class="muted">⏸ 보유 포지션 없음 — 돌파 + 게이트 열림 대기</div></div>')

    # ── shared market state ──
    p.append('<div class="sec-h"><h2>📡 코인별 시장 현황</h2>'
             '<span class="sec-tag">봇과 동일 지표 · 페이지 로드 시점</span></div>')
    if d["market"]:
        p.append('<div class="card"><table><thead><tr><th>심볼</th><th>현재가</th><th>추세(EMA20/50)</th>'
                 '<th>ADX</th><th>ATR%</th><th>롱돌파선(신고가)</th><th>롱까지</th>'
                 '<th>숏돌파선(신저가)</th><th>숏까지</th><th>보유 실/페</th><th>상태</th></tr></thead><tbody>')
        for m in d["market"]:
            tr_cls = "pos" if "상승" in m["trend"] else "neg"
            adx_cls = "pos" if m["trending"] else "mut"
            st_cls = {"pos": "pos", "neg": "neg", "hold": "mut", "mut": "mut"}.get(m["tone"], "mut")
            bl = m.get("brk_lo"); dl = m.get("dist_lo")
            # 진입 임박 강조: 롱후보면 롱까지 초록·굵게, 숏후보면 숏까지 빨강·굵게
            hi_cls = "pos" if m.get("long_cand") else "mono mut"
            lo_cls = "neg" if m.get("short_cand") else "mono mut"
            hl = d["held_live"].get(m["symbol"]); hp = d["held_paper"].get(m["symbol"])
            p.append(f'<tr><td><b>{esc(m["symbol"])}</b></td><td class="mono">{m["price"]:.4f}</td>'
                     f'<td class="{tr_cls}">{m["trend"]}</td>'
                     f'<td class="{adx_cls}">{m["adx"]:.0f}{" ✓" if m["trending"] else ""}</td>'
                     f'<td class="mono">{m["atrp"]:.2f}%</td>'
                     f'<td class="mono">{m["brk"]:.4f}</td><td class="{hi_cls}">{m["dist_hi"]:+.1f}%</td>'
                     f'<td class="mono">{("%.4f" % bl) if bl is not None else "–"}</td>'
                     f'<td class="{lo_cls}">{("%+.1f%%" % dl) if dl is not None else "–"}</td>'
                     f'<td class="mono">실 {_side_html(hl)} · 페 {_side_html(hp)}</td>'
                     f'<td class="{st_cls}">{esc(m["status"])}</td></tr>')
        p.append('</tbody></table>'
                 '<div class="muted" style="margin-top:10px">ls(롱+숏) mixed · ADX&gt;22 추세장만 · '
                 '롱돌파=Donchian 신고가 위로(가격 올라야 진입) / 숏돌파=신저가 아래로(가격 내려야 진입) · '
                 '롱까지/숏까지 = 돌파선까지 거리(작을수록 임박, 0%면 돌파) · 롱=추세종료까지, 숏=2.5R 익절 · '
                 '강한 하락(ADX55+)서 숏 엣지 · 보유 실/페 = 실전·페이퍼 엔진별 보유 방향(별개 표시)</div></div>')
    else:
        p.append('<div class="card"><div class="muted">시장 데이터 로드 실패</div></div>')

    # ── 전체 거래 내역: 4암 통합 단일 테이블 — 청산시각 역순, 암은 배지로 구분.
    #    실전 행 = 굵은 배지 + 옅은 배경(한눈에 튐), 페이퍼 행 = 회색 배지.
    #    같은 신호의 실·페 짝은 청산시각이 붙어 위아래로 나란히 → 슬리피지 비교 즉시 가능.
    _BADGE = {"ls": "페 · 4h", "ls1d": "페 · 1D"}
    merged = []
    for arm in arms:
        nk = arm.get("name_key") or ""
        is_live = nk.startswith("live")
        for t in arm["trades"]:
            # 라이브는 단일 뷰 — 어느 암 거래인지는 거래별 _arm1d 태그로 표기
            tag = ("실 · 1D" if t.get("_arm1d") else "실 · 4h") if is_live else _BADGE.get(nk, arm["label"])
            merged.append(((t.get("closed_utc") or ""), tag, is_live, t))
    merged.sort(key=lambda x: x[0], reverse=True)
    n_live = sum(1 for m in merged if m[2])
    p.append('<div class="sec-h"><h2>전체 거래 내역</h2>'
             f'<span class="sec-tag">4암 통합 {len(merged)}건 (실전 {n_live}) · 최신순 · 슬리피지(신호가 vs 체결) · 스크롤</span></div>')
    p.append('<div class="card" style="max-height:600px;overflow-y:auto;padding:16px 18px">')
    if merged:
        p.append('<table><thead><tr><th>청산</th><th>암</th><th>심볼</th><th>방향</th>'
                 '<th>진입→청산</th><th>슬립</th><th>R</th><th>USD</th><th>사유</th></tr></thead><tbody>')
        for closed, tag, is_live, t in merged:
            r = float(t.get("realized_usdt") or 0); cls = "pos" if r >= 0 else "neg"
            re = float(t.get("ref_entry") or 0); ep = float(t.get("entry_price") or 0)
            slip = f'{(ep - re) / re * 10000:+.0f}bp' if re > 0 else '-'
            row_st = ' style="background:#f3f6f2"' if is_live else ''
            arm_td = f'<b>{tag}</b>' if is_live else f'<span class="mut">{tag}</span>'
            p.append(f'<tr{row_st}><td class="mono">{esc(closed[5:16])}</td>'
                     f'<td style="white-space:nowrap">{arm_td}</td>'
                     f'<td><b>{esc(t["symbol"])}</b></td><td>{"롱" if t["side"]=="Buy" else "숏"}</td>'
                     f'<td class="mono">{(t.get("entry_price") or 0):.4f}→{(t.get("exit_price") or 0):.4f}</td>'
                     f'<td class="mono mut">{slip}</td>'
                     f'<td class="{cls}">{(t.get("realized_R") or 0):+.2f}</td>'
                     f'<td class="{cls}">${r:+.2f}</td><td class="mut">{esc(t.get("close_reason"))}</td></tr>')
        p.append('</tbody></table>')
    else:
        p.append('<div class="muted">아직 거래 없음 — 첫 진입부터 집계</div>')
    p.append('</div>')

    # ── system ──
    p.append('<div class="sec-h"><h2>시스템 상태</h2><span class="sec-tag">systemd</span></div>')
    p.append('<div class="card"><div class="mini-grid">')
    p.append(f'<div><span class="mlbl">engine</span><b>{"ACTIVE" if d["svc"].get("metis-v6") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">dashboard</span><b>{"ACTIVE" if d["svc"].get("metis-v6-dashboard") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">mem used</span><b>{mem} Mi</b></div>')
    p.append(f'<div><span class="mlbl">disk free</span><b>{esc(d["disk"])}</b></div>')
    p.append('</div></div>')

    p.append(f'<footer>METIS-System · {esc(d["mode"])} · 8코인 4h ls(롱+숏) 돌파 mixed · 실전 LIVE + 페이퍼 비교 · USD/KRW {rate:,.0f} · F5</footer>')
    p.append('</div></body></html>')
    return "".join(p)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        try:
            body = render(gather()).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            body = f"error: {e}".encode(); self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    print(f"METIS v6 dashboard on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
