#!/usr/bin/env python3
"""METIS dashboard — zero-dependency monochrome status page (bare http.server).

Renders one static page per request (no auto-refresh, no external price widgets):
- Equity in USD + KRW, daily / cumulative P&L, drawdown, risk state.
- Open positions (up to the concurrency cap) with live mark price and unrealised
  P&L / R per position.
- Recent closed trades, per-coin statistics, and system health.
Mark prices are fetched only for symbols currently held (운영자 spec: no BTC ticker).
"""
from __future__ import annotations

import html as html_lib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import (BYBIT, PAPER_INITIAL_BALANCE_USDT, PAPER_MODE,
                             REAL_SEED_KRW, STATE_DB_PATH, STRATEGY, TRADING)
import sqlite3

import numpy as np

from core.indicators import adx as _adx, atr as _atr, donchian_prev, ema


# ─────────────── helpers ───────────────
def get_usdkrw() -> float:
    try:
        with urlopen("https://api.exchangerate-api.com/v4/latest/USD", timeout=3) as r:
            return float(json.loads(r.read())["rates"]["KRW"])
    except Exception:
        return 1370.0


def krw_str(usd: float, rate: float) -> str:
    return f"₩{usd * rate:+,.0f}".replace("+-", "-")


def q(sql: str, params: tuple = ()) -> list[dict]:
    if not Path(STATE_DB_PATH).exists():
        return []
    conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=5.0)
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


_MARK: dict = {}


def mark_price(symbol: str) -> float:
    """Last price for a held symbol (3s cache). Only called for open positions."""
    now = time.time()
    c = _MARK.get(symbol)
    if c and now - c[0] < 3:
        return c[1]
    try:
        with urlopen(f"{BYBIT.base_url}/v5/market/tickers?category=linear&symbol={symbol}", timeout=3) as r:
            lst = json.loads(r.read()).get("result", {}).get("list", [])
        if lst:
            px = float(lst[0].get("lastPrice") or 0)
            _MARK[symbol] = (now, px)
            return px
    except Exception:
        pass
    return c[1] if c else 0.0


def _fetch_kline(symbol: str) -> list:
    url = (f"{BYBIT.base_url}/v5/market/kline?category=linear&symbol={symbol}"
           f"&interval={STRATEGY.INTERVAL}&limit={STRATEGY.KLINE_LOOKBACK}")
    with urlopen(url, timeout=5) as r:
        return json.loads(r.read()).get("result", {}).get("list", [])


def analyze_coin(symbol: str, held_side: str | None) -> dict | None:
    """Same indicators the engine uses, summarised for the operator: trend, ADX
    regime, ATR%, distance to the breakout levels, and a plain-language status."""
    try:
        rows = sorted(_fetch_kline(symbol), key=lambda r: int(r[0]))[:-1]  # drop forming bar
        if len(rows) < STRATEGY.EMA_SLOW + 5:
            return None
        h = np.array([float(r[2]) for r in rows]); l = np.array([float(r[3]) for r in rows])
        c = np.array([float(r[4]) for r in rows])
        ef = ema(c, STRATEGY.EMA_FAST); es = ema(c, STRATEGY.EMA_SLOW)
        ax = _adx(h, l, c, STRATEGY.ADX_LEN); av = _atr(h, l, c, STRATEGY.ATR_LEN)
        dhi, dlo = donchian_prev(h, l, STRATEGY.DONCHIAN_N)
        i = len(c) - 1
        price = float(c[i]); adxv = float(ax[i]); atrp = float(av[i]) / price * 100 if price else 0
        trending = adxv > STRATEGY.ADX_MIN
        up = ef[i] > es[i]
        dist_hi = (dhi[i] - price) / price * 100 if price else 0   # +면 돌파까지 상승 필요
        dist_lo = (price - dlo[i]) / price * 100 if price else 0   # +면 돌파까지 하락 필요

        if held_side:
            status, tone = ("보유 중 (롱)" if held_side == "Buy" else "보유 중 (숏)"), "hold"
        elif not trending:
            status, tone = f"무추세 관망 (ADX {adxv:.0f})", "mut"
        elif up and price > dhi[i]:
            status, tone = "🟢 상승 돌파 — 롱 후보", "pos"
        elif (not up) and price < dlo[i]:
            status, tone = "🔴 하락 돌파 — 숏 후보", "neg"
        elif up:
            status, tone = f"상승추세 · 고점 +{dist_hi:.1f}% 대기", "mut"
        else:
            status, tone = f"하락추세 · 저점 -{dist_lo:.1f}% 대기", "mut"

        return {"symbol": symbol, "price": price, "trend": "▲ 상승" if up else "▼ 하락",
                "adx": adxv, "trending": trending, "atrp": atrp,
                "dist_hi": dist_hi, "dist_lo": dist_lo, "status": status, "tone": tone}
    except Exception:
        return None


# ─────────────── gather ───────────────
def gather() -> dict:
    d: dict = {}
    eqr = q("SELECT equity_usdt, high_water_usdt FROM equity WHERE key='main'")
    eq = eqr[0]["equity_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    hw = eqr[0]["high_water_usdt"] if eqr else PAPER_INITIAL_BALANCE_USDT
    d["equity"] = eq
    d["high_water"] = hw
    d["initial"] = PAPER_INITIAL_BALANCE_USDT
    d["pnl_usd"] = eq - PAPER_INITIAL_BALANCE_USDT
    d["pnl_pct"] = (d["pnl_usd"] / PAPER_INITIAL_BALANCE_USDT * 100) if PAPER_INITIAL_BALANCE_USDT > 0 else 0
    d["dd_pct"] = ((hw - eq) / hw * 100) if hw > 0 else 0.0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = q("SELECT * FROM daily_pnl WHERE utc_date=?", (today,))
    d["today"] = day[0] if day else {"realized_usdt": 0, "n_trades": 0, "n_wins": 0, "n_losses": 0}

    d["positions"] = q("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_utc")
    held = {p["symbol"]: p["side"] for p in d["positions"]}
    d["market"] = [m for m in (analyze_coin(s, held.get(s)) for s in TRADING.SYMBOLS) if m]
    d["trades"] = q("SELECT * FROM trades ORDER BY id DESC LIMIT 30")
    d["coin_stats"] = q("""
        SELECT symbol, COUNT(*) n, SUM(CASE WHEN realized_usdt>0 THEN 1 ELSE 0 END) wins,
               AVG(realized_R) avg_R, SUM(realized_usdt) sum_pnl
        FROM trades GROUP BY symbol ORDER BY sum_pnl DESC""")
    rs = q("SELECT * FROM risk_flags WHERE key='global'")
    d["risk"] = rs[0] if rs else {"manual_kill": 0, "reason": None}
    last = q("SELECT ts_utc FROM journal ORDER BY id DESC LIMIT 1")
    d["last_event_utc"] = last[0]["ts_utc"] if last else None

    d["svc"] = {s: is_active(s) for s in ("metis-v6", "metis-v6-dashboard")}
    d["mem_mi"] = free_mem_mi()
    d["disk"] = disk_pct()
    d["usdkrw"] = get_usdkrw()
    d["now_kst"] = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")
    d["mode"] = "PAPER" if PAPER_MODE else "LIVE"
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
.hero{background:#fff;border-radius:18px;padding:28px 32px;margin-bottom:26px;box-shadow:0 1px 3px rgba(0,0,0,.04),0 0 0 1px rgba(15,23,42,.05)}
.hero-grid{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:34px}
.hero-cell+.hero-cell{border-left:1px solid #f0eee7;padding-left:30px}
.hero-l{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af;margin-bottom:12px}
.hero-v{font-size:34px;font-weight:800;letter-spacing:-.025em;font-variant-numeric:tabular-nums;color:#0f172a}
.hero-v.small{font-size:22px}
.hero-sub{margin-top:10px;font-size:13px;color:#4b5563}
.hero-krw{margin-top:4px;font-size:12px;color:#9ca3af}
.pos{color:#059669;font-weight:700}.neg{color:#dc2626;font-weight:700}.mut{color:#6b7280;font-weight:600}
.muted{color:#9ca3af;font-size:13px}
.big{font-size:40px;font-weight:800;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}
th{font-size:10.5px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:10px 12px;border-bottom:1px solid #f1f0eb;background:#fafaf6}
td{padding:11px 12px;font-size:12.5px;border-bottom:1px solid #f6f5f0}
td.mono{font-family:'SF Mono',Menlo,monospace;font-size:11.5px;color:#6b7280}
.badge-on{padding:4px 10px;font-size:11px;font-weight:700;background:#d1fae5;color:#065f46;border-radius:6px}
.badge-bad{padding:4px 10px;font-size:11px;font-weight:700;background:#fee2e2;color:#b91c1c;border-radius:6px}
.mlbl{display:block;font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:3px}
.mini-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px 18px}
footer{margin-top:40px;padding-top:20px;border-top:1px solid #f0eee7;font-size:11.5px;color:#9ca3af;text-align:center}
"""


def render(d: dict) -> str:
    rate = d["usdkrw"]
    eq = d["equity"]; pnl = d["pnl_usd"]
    pcls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "mut")
    arrow = "▲" if pnl > 0 else ("▼" if pnl < 0 else "·")
    tnet = d["today"].get("realized_usdt", 0) or 0
    tcls = "pos" if tnet > 0 else ("neg" if tnet < 0 else "mut")
    manual = int(d["risk"].get("manual_kill", 0) or 0)
    risk_badge = '<span class="badge-bad">MANUAL KILL</span>' if manual else '<span class="badge-on">OK</span>'
    mem = d["mem_mi"]
    on, off = '<span class="svc-dot svc-on"></span>', '<span class="svc-dot svc-off"></span>'

    # unrealised P&L across open positions
    upnl = 0.0
    pos_rows = []
    for p in d["positions"]:
        mk = mark_price(p["symbol"])
        dd = 1 if p["side"] == "Buy" else -1
        u = dd * (mk - p["entry_price"]) * p["qty"] if mk > 0 else 0.0
        risk_usdt = abs(p["entry_price"] - p["stop_price"]) * p["qty"]
        r_now = u / risk_usdt if risk_usdt > 0 else 0.0
        upnl += u
        ucls = "pos" if u >= 0 else "neg"
        pos_rows.append(
            f'<tr><td><b>{esc(p["symbol"])}</b></td>'
            f'<td>{"롱" if p["side"]=="Buy" else "숏"} x{p["leverage"]}</td>'
            f'<td class="mono">{p["qty"]}</td>'
            f'<td class="mono">{p["entry_price"]:.4f}</td>'
            f'<td class="mono">{p["stop_price"]:.4f}</td>'
            f'<td class="mono">{mk:.4f}</td>'
            f'<td class="{ucls}">${u:+.2f}</td>'
            f'<td class="{ucls}">{r_now:+.2f}R</td>'
            f'<td class="mono">{esc((p["opened_utc"] or "")[:16])}</td></tr>')
    ucls = "pos" if upnl >= 0 else "neg"

    p: list[str] = [f'<!doctype html><html><head><meta charset="utf-8"><title>METIS · {d["mode"]}</title>',
                    f'<style>{CSS}</style></head><body><div class="wrap">']

    # topbar
    p.append(
        '<div class="topbar"><div class="brand"><div class="brand-mark">운영자</div>'
        '<div class="brand-divider"></div><div><div class="brand-title">DELTA OMEGA '
        f'<span class="mode">// {d["mode"]}</span></div>'
        '<div class="brand-sub">8-coin 4h breakout portfolio</div></div></div>'
        '<div class="topmeta">'
        f'<span><span class="lbl">engine</span> {on if d["svc"].get("metis-v6") else off}</span>'
        f'<span><span class="lbl">fx</span> <b>₩{rate:,.0f}</b></span>'
        f'<span><span class="lbl">mem</span> <b>{mem}Mi</b></span>'
        f'<span><span class="lbl">disk</span> <b>{esc(d["disk"])}</b></span>'
        f'<span><span class="lbl">kst</span> <b>{esc(d["now_kst"])}</b></span>'
        '</div></div>')

    # current positions
    p.append(f'<div class="sec-h" style="margin-top:0"><h2>🎯 현재 포지션</h2>'
             f'<span class="sec-tag">{len(d["positions"])}/{4} 보유 · 미실현 실시간</span></div>')
    p.append('<div class="card">')
    p.append(f'<div class="mlbl">총 미실현 PnL</div>'
             f'<div class="big {ucls}">${upnl:+,.2f}</div>'
             f'<div class="hero-krw"><span class="{ucls}">{krw_str(upnl, rate)}</span></div>')
    if pos_rows:
        p.append('<table style="margin-top:16px"><thead><tr><th>심볼</th><th>방향</th><th>수량</th>'
                 '<th>진입</th><th>SL</th><th>현재가</th><th>미실현$</th><th>R</th><th>진입시각</th></tr></thead><tbody>')
        p.append("".join(pos_rows)); p.append('</tbody></table>')
    else:
        p.append('<div class="muted" style="margin-top:14px">⏸ 보유 포지션 없음 — 다음 4h cycle에 돌파 대기</div>')
    p.append('</div>')

    # hero
    p.append('<div class="hero"><div class="hero-grid">')
    p.append('<div class="hero-cell"><div class="hero-l">총 자산 (PAPER)</div>'
             f'<div class="hero-v">${eq:,.2f}</div>'
             f'<div class="hero-sub"><span class="{pcls}">{arrow} ${abs(pnl):,.2f} ({d["pnl_pct"]:+.2f}%)</span> · 원금 ${d["initial"]:,.0f}</div>'
             f'<div class="hero-krw">{krw_str(eq, rate)} · 손익 {krw_str(pnl, rate)}</div></div>')
    p.append('<div class="hero-cell"><div class="hero-l">오늘 P&L</div>'
             f'<div class="hero-v small {tcls}">${tnet:+.2f}</div>'
             f'<div class="hero-sub">{d["today"].get("n_trades",0)} trades · W {d["today"].get("n_wins",0)} L {d["today"].get("n_losses",0)}</div>'
             f'<div class="hero-krw">{krw_str(tnet, rate)}</div></div>')
    p.append('<div class="hero-cell"><div class="hero-l">Drawdown</div>'
             f'<div class="hero-v small">{d["dd_pct"]:.2f}%</div>'
             f'<div class="hero-sub">HW ${d["high_water"]:,.2f}</div></div>')
    p.append('<div class="hero-cell"><div class="hero-l">Risk State</div>'
             f'<div class="hero-v small">{risk_badge}</div>'
             f'<div class="hero-sub">{esc((d["risk"].get("reason") or "-")[:48])}</div></div>')
    p.append('</div></div>')

    # per-coin market state (same indicators the engine uses)
    p.append('<div class="sec-h"><h2>📡 코인별 시장 현황</h2>'
             '<span class="sec-tag">봇과 동일 지표 · 페이지 로드 시점</span></div>')
    if d["market"]:
        p.append('<div class="card"><table><thead><tr><th>심볼</th><th>현재가</th><th>추세(EMA20/50)</th>'
                 '<th>ADX</th><th>ATR%</th><th>고점 돌파까지</th><th>저점 돌파까지</th><th>봇 판단</th></tr></thead><tbody>')
        for m in d["market"]:
            tr_cls = "pos" if "상승" in m["trend"] else "neg"
            adx_cls = "pos" if m["trending"] else "mut"
            st_cls = {"pos": "pos", "neg": "neg", "hold": "mut", "mut": "mut"}.get(m["tone"], "mut")
            p.append(f'<tr><td><b>{esc(m["symbol"])}</b></td>'
                     f'<td class="mono">{m["price"]:.4f}</td>'
                     f'<td class="{tr_cls}">{m["trend"]}</td>'
                     f'<td class="{adx_cls}">{m["adx"]:.0f}{" ✓" if m["trending"] else ""}</td>'
                     f'<td class="mono">{m["atrp"]:.2f}%</td>'
                     f'<td class="mono">{m["dist_hi"]:+.1f}%</td>'
                     f'<td class="mono">{m["dist_lo"]:+.1f}%</td>'
                     f'<td class="{st_cls}">{esc(m["status"])}</td></tr>')
        p.append('</tbody></table>'
                 '<div class="muted" style="margin-top:10px">ADX>22 = 추세장(진입 가능)·미만은 관망 · '
                 '고점/저점 돌파까지 = Donchian 돌파선까지 거리(+는 아직 못 닿음) · 추세 방향으로 돌파하면 진입 후보</div></div>')
    else:
        p.append('<div class="card"><div class="muted">시장 데이터 로드 실패</div></div>')

    # recent trades
    p.append('<div class="sec-h"><h2>최근 거래</h2><span class="sec-tag">closed</span></div>')
    if d["trades"]:
        p.append('<div class="card"><table><thead><tr><th>청산(UTC)</th><th>심볼</th><th>방향</th>'
                 '<th>진입→청산</th><th>R</th><th>USD</th><th>KRW</th><th>사유</th></tr></thead><tbody>')
        for t in d["trades"]:
            r = float(t.get("realized_usdt") or 0); cls = "pos" if r >= 0 else "neg"
            p.append(f'<tr><td class="mono">{esc((t.get("closed_utc") or "")[5:16])}</td>'
                     f'<td>{esc(t["symbol"])}</td><td>{"롱" if t["side"]=="Buy" else "숏"}</td>'
                     f'<td class="mono">{(t.get("entry_price") or 0):.4f}→{(t.get("exit_price") or 0):.4f}</td>'
                     f'<td class="{cls}">{(t.get("realized_R") or 0):+.2f}</td>'
                     f'<td class="{cls}">${r:+.2f}</td><td class="{cls}">{krw_str(r, rate)}</td>'
                     f'<td class="mut">{esc(t.get("close_reason"))}</td></tr>')
        p.append('</tbody></table></div>')
    else:
        p.append('<div class="card"><div class="muted">청산된 거래 없음</div></div>')

    # per-coin stats
    p.append('<div class="sec-h"><h2>코인별 통계</h2><span class="sec-tag">all-time</span></div>')
    if d["coin_stats"]:
        p.append('<div class="card"><table><thead><tr><th>심볼</th><th>n</th><th>WR%</th>'
                 '<th>avg R</th><th>누적 USD</th><th>누적 KRW</th></tr></thead><tbody>')
        for s in d["coin_stats"]:
            n = s["n"] or 0; wr = (s["wins"] or 0) / n * 100 if n else 0
            sp = float(s["sum_pnl"] or 0); cls = "pos" if sp >= 0 else "neg"
            p.append(f'<tr><td><b>{esc(s["symbol"])}</b></td><td>{n}</td><td>{wr:.0f}</td>'
                     f'<td>{(s["avg_R"] or 0):+.2f}</td><td class="{cls}">${sp:+.2f}</td>'
                     f'<td class="{cls}">{krw_str(sp, rate)}</td></tr>')
        p.append('</tbody></table></div>')
    else:
        p.append('<div class="card"><div class="muted">아직 거래 통계 없음</div></div>')

    # system
    p.append('<div class="sec-h"><h2>시스템 상태</h2><span class="sec-tag">systemd</span></div>')
    p.append('<div class="card"><div class="mini-grid">')
    p.append(f'<div><span class="mlbl">engine</span><b>{"ACTIVE" if d["svc"].get("metis-v6") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">dashboard</span><b>{"ACTIVE" if d["svc"].get("metis-v6-dashboard") else "DOWN"}</b></div>')
    p.append(f'<div><span class="mlbl">mem used</span><b>{mem} Mi</b></div>')
    p.append(f'<div><span class="mlbl">disk free</span><b>{esc(d["disk"])}</b></div>')
    p.append(f'<div><span class="mlbl">last event (UTC)</span><b>{esc((d["last_event_utc"] or "-")[:19])}</b></div>')
    p.append('</div></div>')

    p.append(f'<footer>METIS v6 · {esc(d["mode"])} · 8-coin 4h breakout · USD/KRW {rate:,.0f} · bare http.server · F5 새로고침</footer>')
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
