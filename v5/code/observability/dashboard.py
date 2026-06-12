#!/usr/bin/env python3
"""METIS dashboard — zero-dependency status page.

Bare ``http.server``-based dashboard (~20 MB RAM) that renders:

- Equity in USD and KRW, with daily and cumulative P&L.
- The current open position (if any).
- The most recent AI decisions (last 50) with the raw feature input
  available in a collapsible ``<details>`` block.
- Per-setup statistics, recent trade outcomes, and system health.
- USD/KRW rate auto-fetched from a public exchange-rate API.
"""
from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    BYBIT, BYBIT_REAL_SEED_KRW, BYBIT_REAL_SEED_USDT, PAPER_INITIAL_BALANCE_USDT,
    PAPER_MODE, STATE_DB_PATH, TELEMETRY_DB_PATH, TRADING,
)


# ─────────────────────────── helpers ───────────────────────────
def get_usdkrw() -> float:
    try:
        with urlopen("https://api.exchangerate-api.com/v4/latest/USD", timeout=3) as r:
            return float(json.loads(r.read())["rates"]["KRW"])
    except Exception:
        return 1370.0


def krw_str(usd: float, rate: float) -> str:
    krw = usd * rate
    return f"₩{krw:+,.0f}".replace("+-", "-")


def q(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
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
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if line.startswith("Mem:"):
                return int(line.split()[2])
    except Exception:
        pass
    return 0


def disk_pct() -> str:
    try:
        out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=2).stdout
        return out.splitlines()[-1].split()[3]
    except Exception:
        return "?"


def esc(s) -> str:
    if s is None:
        return ""
    return html_lib.escape(str(s))


def color_class(v: float) -> str:
    if v is None or v == 0: return "mut"
    return "pos" if v > 0 else "neg"


# ─────────────────────────── Bybit real-time wallet ───────────────────────────
_BTC_PRICE_CACHE: dict = {"ts": 0.0, "price": 0.0, "chg24": 0.0}
_WALLET_CACHE: dict = {"ts": 0.0, "data": None}
_DEPOSIT_CACHE: dict = {"ts": 0.0, "data": None}  # long cache — deposit history rarely changes
_MARK_CACHE: dict = {}  # symbol → (ts, price)


def get_mark_price(symbol: str) -> float:
    """Current mark/last price for the position symbol. 3s cache."""
    now = time.time()
    cached = _MARK_CACHE.get(symbol)
    if cached and now - cached[0] < 3:
        return cached[1]
    try:
        with urlopen(f"{BYBIT.BASE_URL}/v5/market/tickers?category=linear&symbol={symbol}", timeout=3) as r:
            data = json.loads(r.read())
        item = data.get("result", {}).get("list", [])
        if item:
            price = float(item[0].get("markPrice") or item[0].get("lastPrice") or 0)
            _MARK_CACHE[symbol] = (now, price)
            return price
    except Exception:
        pass
    return cached[1] if cached else 0.0


def get_bybit_btc_price() -> tuple[float, float]:
    """BTC/USDT spot price + 24h change %. ~5s cache."""
    now = time.time()
    if now - _BTC_PRICE_CACHE["ts"] < 5:
        return _BTC_PRICE_CACHE["price"], _BTC_PRICE_CACHE["chg24"]
    try:
        with urlopen(f"{BYBIT.BASE_URL}/v5/market/tickers?category=spot&symbol=BTCUSDT", timeout=4) as r:
            data = json.loads(r.read())
        item = data.get("result", {}).get("list", [])
        if item:
            price = float(item[0].get("lastPrice") or 0)
            chg24 = float(item[0].get("price24hPcnt") or 0) * 100
            _BTC_PRICE_CACHE.update(ts=now, price=price, chg24=chg24)
            return price, chg24
    except Exception:
        pass
    return _BTC_PRICE_CACHE["price"], _BTC_PRICE_CACHE["chg24"]


def _bybit_signed_get(path: str, qs: str, timeout: float = 6.0) -> dict:
    """Helper for signed GET. Returns parsed JSON or raises."""
    if not BYBIT.API_KEY or not BYBIT.SECRET:
        raise RuntimeError("no_api_key")
    ts = str(int(time.time() * 1000))
    recv = str(BYBIT.RECV_WINDOW_MS)
    msg = ts + BYBIT.API_KEY + recv + qs
    sig = hmac.new(BYBIT.SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    url = f"{BYBIT.BASE_URL}{path}?{qs}"
    req = Request(url, headers={
        "X-BAPI-API-KEY": BYBIT.API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sig,
    })
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_bybit_net_deposits() -> dict:
    """Sum of all-time deposits - withdrawals. Cached 5 min (history rarely changes).

    Returns:
      {
        "net_usdt": float (USDT/USDC/stable deposits net),
        "net_other_coins_value_usd": float (BTC/ETH 등을 *현재 가격*으로 환산 — 근사),
        "total_seed_usd": float (위 둘 합산),
        "deposits": [...], "withdraws": [...],
        "warning": str | None,
        "error": str | None,
      }
    """
    now = time.time()
    if now - _DEPOSIT_CACHE["ts"] < 300 and _DEPOSIT_CACHE["data"] is not None:
        return _DEPOSIT_CACHE["data"]

    out: dict = {
        "net_usdt": 0.0,
        "net_other_coins_value_usd": 0.0,
        "total_seed_usd": 0.0,
        "deposits": [],
        "withdraws": [],
        "warning": None,
        "error": None,
    }
    try:
        # Deposits — paginate via cursor. Bybit V5: limit max 50.
        deposits: list[dict] = []
        cursor = ""
        for _ in range(20):  # max 20 pages = 1000 records
            qs = f"limit=50"
            if cursor:
                qs += f"&cursor={cursor}"
            data = _bybit_signed_get("/v5/asset/deposit/query-record", qs)
            if str(data.get("retCode")) != "0":
                out["error"] = f"deposit retCode={data.get('retCode')} {data.get('retMsg')}"
                break
            rows = data.get("result", {}).get("rows", [])
            deposits.extend(rows)
            cursor = data.get("result", {}).get("nextPageCursor", "")
            if not cursor or not rows:
                break

        # Withdraws (only confirmed)
        withdraws: list[dict] = []
        cursor = ""
        for _ in range(20):
            qs = f"limit=50&withdrawType=2"  # 0 on-chain, 1 off-chain, 2 all
            if cursor:
                qs += f"&cursor={cursor}"
            try:
                data = _bybit_signed_get("/v5/asset/withdraw/query-record", qs)
                if str(data.get("retCode")) != "0":
                    break
                rows = data.get("result", {}).get("rows", [])
                withdraws.extend(rows)
                cursor = data.get("result", {}).get("nextPageCursor", "")
                if not cursor or not rows:
                    break
            except Exception:
                break

        # Aggregate per coin
        per_coin: dict[str, float] = {}
        for r in deposits:
            if str(r.get("status")) not in ("3", 3):  # 3 = success on Bybit V5
                continue
            try:
                coin = r.get("coin") or ""
                amt = float(r.get("amount", 0) or 0)
                per_coin[coin] = per_coin.get(coin, 0.0) + amt
            except (TypeError, ValueError):
                continue
        for r in withdraws:
            if str(r.get("status")) not in ("success", "SUCCESS", "2"):
                continue
            try:
                coin = r.get("coin") or ""
                amt = float(r.get("amount", 0) or 0)
                per_coin[coin] = per_coin.get(coin, 0.0) - amt
            except (TypeError, ValueError):
                continue

        out["deposits"] = deposits[:20]  # only keep recent 20 for display
        out["withdraws"] = withdraws[:20]
        out["per_coin_net"] = per_coin

        # Convert: stable coins 1:1, others via current spot price (approx)
        stable = {"USDT", "USDC", "DAI", "BUSD", "FDUSD"}
        for coin, amt in per_coin.items():
            if coin in stable:
                out["net_usdt"] += amt
            elif coin == "USD":
                out["net_usdt"] += amt
            else:
                # Use current spot price as rough estimate (deposit-time price unknown without extra calls)
                try:
                    with urlopen(f"{BYBIT.BASE_URL}/v5/market/tickers?category=spot&symbol={coin}USDT", timeout=3) as r:
                        td = json.loads(r.read())
                    px = float(td.get("result", {}).get("list", [{}])[0].get("lastPrice") or 0)
                    out["net_other_coins_value_usd"] += amt * px
                except Exception:
                    pass

        out["total_seed_usd"] = out["net_usdt"] + out["net_other_coins_value_usd"]
        if out["net_other_coins_value_usd"] > 0:
            out["warning"] = "non-stable deposits (BTC/ETH 등) 는 *현재* 가격으로 환산 — 입금 시점가 아님 (P&L 근사)"

        _DEPOSIT_CACHE.update(ts=now, data=out)
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def get_bybit_wallet() -> dict:
    """Fetch full Bybit UNIFIED wallet. ~10s cache (signed call)."""
    now = time.time()
    if now - _WALLET_CACHE["ts"] < 10 and _WALLET_CACHE["data"] is not None:
        return _WALLET_CACHE["data"]
    if not BYBIT.API_KEY or not BYBIT.SECRET:
        return {"error": "no_api_key"}
    try:
        ts = str(int(time.time() * 1000))
        recv = str(BYBIT.RECV_WINDOW_MS)
        qs = "accountType=UNIFIED"
        msg = ts + BYBIT.API_KEY + recv + qs
        sig = hmac.new(BYBIT.SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        url = f"{BYBIT.BASE_URL}/v5/account/wallet-balance?{qs}"
        req = Request(url, headers={
            "X-BAPI-API-KEY": BYBIT.API_KEY,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sig,
        })
        with urlopen(req, timeout=5) as r:
            raw = json.loads(r.read())
        if str(raw.get("retCode")) != "0":
            return {"error": f"retCode={raw.get('retCode')} {raw.get('retMsg', '')}"}
        result = raw.get("result", {}).get("list", [{}])[0]
        coins_raw = result.get("coin") or []
        coins = []
        for c in coins_raw:
            try:
                bal = float(c.get("walletBalance") or 0)
                if abs(bal) < 1e-9:
                    continue
                usd = float(c.get("usdValue") or 0)
                coins.append({
                    "coin": c.get("coin"),
                    "balance": bal,
                    "usd_value": usd,
                    "available": float(c.get("availableToWithdraw") or 0),
                    "locked": float(c.get("locked") or 0),
                    "unrealised_pnl": float(c.get("unrealisedPnl") or 0),
                })
            except (TypeError, ValueError):
                continue
        coins.sort(key=lambda x: -x["usd_value"])
        out = {
            "total_equity": float(result.get("totalEquity") or 0),
            "total_wallet_balance": float(result.get("totalWalletBalance") or 0),
            "total_unrealised_pnl": float(result.get("totalPerpUPL") or 0),
            "total_available": float(result.get("totalAvailableBalance") or 0),
            "coins": coins,
        }
        _WALLET_CACHE.update(ts=now, data=out)
        return out
    except Exception as e:
        return {"error": str(e)[:120]}


# ─────────────────────────── data gather ───────────────────────────
def gather() -> dict:
    out: dict = {}
    # equity
    eq_rows = q(STATE_DB_PATH, "SELECT ts_utc, equity_usdt, high_water_usdt FROM equity_marker ORDER BY ts_utc DESC LIMIT 200")
    if eq_rows:
        out["equity"] = eq_rows[0]["equity_usdt"]
        out["high_water"] = eq_rows[0]["high_water_usdt"]
        out["equity_history"] = list(reversed(eq_rows))
    else:
        out["equity"] = PAPER_INITIAL_BALANCE_USDT
        out["high_water"] = PAPER_INITIAL_BALANCE_USDT
        out["equity_history"] = []
    out["initial"] = PAPER_INITIAL_BALANCE_USDT
    out["pnl_usd"] = out["equity"] - PAPER_INITIAL_BALANCE_USDT
    out["pnl_pct"] = (out["pnl_usd"] / PAPER_INITIAL_BALANCE_USDT * 100) if PAPER_INITIAL_BALANCE_USDT > 0 else 0
    if out["high_water"] > 0:
        out["dd_pct"] = (out["high_water"] - out["equity"]) / out["high_water"] * 100
    else:
        out["dd_pct"] = 0.0

    # today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = q(STATE_DB_PATH, "SELECT * FROM daily_pnl WHERE utc_date = ?", (today,))
    out["today"] = day[0] if day else {"realized_usdt": 0, "fees_usdt": 0, "funding_usdt": 0,
                                        "n_trades": 0, "n_wins": 0, "n_losses": 0,
                                        "kill_triggered": 0}
    out["today_net"] = (out["today"].get("realized_usdt", 0) or 0) + \
                       (out["today"].get("fees_usdt", 0) or 0) + \
                       (out["today"].get("funding_usdt", 0) or 0)

    # 7d
    week_rows = q(STATE_DB_PATH, "SELECT * FROM daily_pnl ORDER BY utc_date DESC LIMIT 7")
    out["week"] = week_rows

    # risk state
    rs = q(STATE_DB_PATH, "SELECT * FROM risk_state WHERE key='global'")
    out["risk"] = rs[0] if rs else {}

    # position
    pos_row = q(STATE_DB_PATH, "SELECT value_json FROM state_snapshot WHERE key='open_position'")
    if pos_row:
        try:
            out["position"] = json.loads(pos_row[0]["value_json"])
        except Exception:
            out["position"] = {}
    else:
        out["position"] = {}

    # system snapshots
    for skey in ("ws_health", "time_sync", "health"):
        row = q(STATE_DB_PATH, "SELECT value_json FROM state_snapshot WHERE key=?", (skey,))
        if row:
            try:
                out[skey] = json.loads(row[0]["value_json"])
            except Exception:
                out[skey] = {}
        else:
            out[skey] = {}

    # cycles (recent 50)
    out["cycles"] = q(TELEMETRY_DB_PATH,
                     "SELECT * FROM cycle_events ORDER BY cycle_event_id DESC LIMIT 50")

    # outcomes
    out["outcomes"] = q(TELEMETRY_DB_PATH,
                       "SELECT * FROM trade_outcomes ORDER BY outcome_id DESC LIMIT 30")

    # setup stats (30d)
    out["setup_stats"] = q(TELEMETRY_DB_PATH, """
        SELECT setup_id, COUNT(*) AS n, SUM(won) AS wins,
               AVG(realized_R) AS avg_R, SUM(realized_pnl_usdt) AS sum_pnl,
               AVG(realized_pnl_usdt) AS avg_pnl, AVG(time_in_trade_min) AS avg_min
        FROM trade_outcomes GROUP BY setup_id ORDER BY n DESC
    """)

    # confidence buckets
    out["conf_buckets"] = q(TELEMETRY_DB_PATH, """
        SELECT confidence_bucket AS bucket, COUNT(*) AS n,
               SUM(CASE WHEN decision IN ('ENTER_LONG','ENTER_SHORT') THEN 1 ELSE 0 END) AS entered
        FROM cycle_events
        WHERE confidence_bucket IS NOT NULL
        GROUP BY confidence_bucket ORDER BY confidence_bucket
    """)

    # services
    out["svc"] = {sv: is_active(sv) for sv in
                 ("metis-v5", "metis-v5-dashboard", "metis-v4-bot")}
    out["mem_mi"] = free_mem_mi()
    out["disk"] = disk_pct()
    out["usdkrw"] = get_usdkrw()
    out["now_kst"] = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    out["mode"] = "PAPER" if PAPER_MODE else "LIVE"
    out["symbols"] = list(TRADING.SYMBOLS)
    return out


# ─────────────────────────── CSS — cream background, white cards, indigo accent ───────────────────────────
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: #faf8f4; color: #1a1d24; font-family: -apple-system, 'SF Pro Display', 'Inter', system-ui, sans-serif; font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1480px; margin: 0 auto; padding: 28px 32px 80px; }

/* ── 운영자 topbar — modern monochrome (aligned) ── */
.topbar { display: flex; align-items: center; justify-content: space-between; padding: 20px 28px; background: #ffffff; border-radius: 12px; box-shadow: 0 0 0 1px rgba(15,23,42,0.06); margin-bottom: 28px; gap: 24px; }
.brand { display: flex; align-items: center; gap: 18px; min-height: 48px; }
.brand-mark { font-size: 36px; font-weight: 900; line-height: 1; letter-spacing: -0.04em; color: #0f172a; font-family: -apple-system, 'SF Pro Display', 'Inter', system-ui, sans-serif; }
.brand-divider { width: 1px; align-self: stretch; background: #e5e7eb; margin: 4px 0; }
.brand-text { display: flex; flex-direction: column; justify-content: center; gap: 5px; }
.brand-title { font-size: 13px; font-weight: 700; letter-spacing: 0.22em; color: #0f172a; font-family: 'SF Mono', 'Menlo', ui-monospace, monospace; text-transform: uppercase; line-height: 1; }
.brand-title .mode { color: #9ca3af; font-weight: 600; margin-left: 6px; }
.brand-sub { font-size: 11px; color: #9ca3af; font-weight: 500; letter-spacing: 0.02em; font-family: 'SF Mono', monospace; line-height: 1; }
.topmeta { display: flex; align-items: center; gap: 22px; font-size: 11.5px; color: #4b5563; flex-wrap: wrap; font-family: 'SF Mono', 'Menlo', ui-monospace, monospace; }
.topmeta > span { display: inline-flex; align-items: center; gap: 8px; line-height: 1; height: 18px; }
.topmeta b { color: #0f172a; font-weight: 700; }
.topmeta .lbl { color: #9ca3af; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; font-size: 10px; }
.topmeta .svc-dot { width: 8px; height: 8px; border-radius: 50%; }
.topmeta .svc-on { background: #10b981; box-shadow: 0 0 0 3px rgba(16,185,129,0.18); }
.topmeta .svc-off { background: #d1d5db; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #10b981; box-shadow: 0 0 0 3px rgba(16,185,129,0.20); vertical-align: middle; }

.hero { background: #ffffff; border-radius: 18px; padding: 30px 34px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); }
.hero-grid { display: grid; grid-template-columns: 1.6fr 1fr 1fr 1fr; gap: 36px; }
.hero-cell { display: flex; flex-direction: column; }
.hero-cell + .hero-cell { border-left: 1px solid #f0eee7; padding-left: 32px; }
.hero-l { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #9ca3af; margin-bottom: 12px; }
.hero-v { font-size: 36px; font-weight: 800; letter-spacing: -0.025em; line-height: 1.05; font-variant-numeric: tabular-nums; color: #0f172a; }
.hero-v.pos { color: #059669; }
.hero-v.neg { color: #dc2626; }
.hero-v.small { font-size: 22px; }
.hero-sub { margin-top: 10px; font-size: 13px; font-weight: 500; color: #4b5563; }
.hero-krw { margin-top: 4px; font-size: 12px; color: #9ca3af; font-weight: 500; }

.sec-h { display: flex; align-items: baseline; gap: 12px; margin: 32px 0 14px; padding: 0 4px; }
.sec-h h2 { font-size: 19px; font-weight: 700; letter-spacing: -0.015em; color: #0f172a; }
.sec-tag { font-size: 11px; color: #9ca3af; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }

.card { background: #ffffff; border-radius: 14px; padding: 22px 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); margin-bottom: 14px; }
.card-h { font-size: 16px; font-weight: 700; color: #0f172a; margin-bottom: 4px; letter-spacing: -0.01em; }
.card-sub { font-weight: 500; color: #9ca3af; font-size: 13px; }
.desc { font-size: 12.5px; color: #6b7280; margin-bottom: 16px; line-height: 1.55; }

.mini-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px 18px; padding: 12px 0; border-top: 1px dashed #f0eee7; margin-top: 8px; }
.mini-grid div { font-size: 13px; }
.mlbl { display: block; font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 700; margin-bottom: 3px; }

.badge-on  { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #d1fae5; color: #065f46; border-radius: 6px; }
.badge-off { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #f3f4f6; color: #6b7280; border-radius: 6px; }
.badge-warn{ display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #fef3c7; color: #92400e; border-radius: 6px; }
.badge-bad { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #fee2e2; color: #b91c1c; border-radius: 6px; }
.pos { color: #059669; font-weight: 700; }
.neg { color: #dc2626; font-weight: 700; }
.mut { color: #6b7280; font-weight: 600; }
.muted { color: #9ca3af; font-size: 12px; }

.svc-grid { display: flex; flex-wrap: wrap; gap: 10px; }
.svc-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; background: #faf8f4; border-radius: 10px; font-size: 12.5px; font-weight: 600; color: #1a1d24; box-shadow: 0 0 0 1px rgba(15,23,42,0.06); }
.svc-dot { width: 8px; height: 8px; border-radius: 50%; }
.svc-on { background: #10b981; box-shadow: 0 0 0 3px rgba(16,185,129,0.20); }
.svc-off { background: #d1d5db; }

table { width: 100%; border-collapse: collapse; }
th { font-size: 10.5px; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; text-align: left; padding: 10px 12px; border-bottom: 1px solid #f1f0eb; background: #fafaf6; }
td { padding: 11px 12px; font-size: 12.5px; color: #1a1d24; border-bottom: 1px solid #f6f5f0; vertical-align: top; }
td.mono { font-family: 'SF Mono', 'Menlo', monospace; font-size: 11.5px; color: #6b7280; }
tr.cyc-enter td { background: #f0fdf4; }
tr.cyc-veto td { background: #fff7e6; }
tr.cyc-no td  { background: #fafafa; }

/* ── AI 판단 카드 — 새 디자인 ── */
.cyc-card { background: #ffffff; border-radius: 12px; margin: 10px 0; padding: 16px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); border-left: 4px solid #d1d5db; transition: box-shadow 0.15s; }
.cyc-card.long  { border-left-color: #10b981; }
.cyc-card.short { border-left-color: #dc2626; }
.cyc-card.veto  { border-left-color: #f59e0b; }
.cyc-card.no    { border-left-color: #d1d5db; }
.cyc-head { display: grid; grid-template-columns: 80px 90px 1fr auto; gap: 16px; align-items: center; margin-bottom: 10px; }
.cyc-ts { font-family: 'SF Mono', 'Menlo', monospace; font-size: 12px; color: #9ca3af; font-weight: 600; }
.cyc-sym { font-size: 14px; font-weight: 800; color: #0f172a; letter-spacing: -0.01em; }
.cyc-tag { display: inline-block; padding: 5px 12px; font-size: 12px; font-weight: 800; border-radius: 6px; letter-spacing: 0.02em; }
.cyc-tag.long  { background: #d1fae5; color: #065f46; }
.cyc-tag.short { background: #fee2e2; color: #b91c1c; }
.cyc-tag.veto  { background: #fef3c7; color: #92400e; }
.cyc-tag.no    { background: #f3f4f6; color: #6b7280; }
.cyc-meta-mid { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; font-size: 12px; color: #4b5563; }
.cyc-meta-mid b { color: #0f172a; font-weight: 700; }
.cyc-meta-mid .chip { padding: 3px 8px; background: #faf8f4; border-radius: 5px; font-size: 11px; font-weight: 600; color: #4b5563; box-shadow: 0 0 0 1px rgba(15,23,42,0.04); }
.cyc-meta-right { font-size: 11px; color: #9ca3af; font-family: 'SF Mono', monospace; text-align: right; line-height: 1.4; font-weight: 600; }
.cyc-body { padding: 12px 0 4px 0; border-top: 1px dashed #f0eee7; margin-top: 8px; }
.cyc-evidence { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 18px; margin-bottom: 8px; }
.cyc-evidence.single { grid-template-columns: 1fr; }
.cyc-ev { padding: 8px 12px; background: #faf8f4; border-radius: 8px; border-left: 3px solid #4f46e5; }
.cyc-ev.tech    { border-left-color: #4f46e5; }
.cyc-ev.sup     { border-left-color: #f59e0b; }
.cyc-ev.veto    { border-left-color: #dc2626; }
.cyc-ev .lbl { font-size: 10px; color: #6b7280; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; }
.cyc-ev .val { font-size: 12.5px; color: #1a1d24; font-weight: 500; line-height: 1.5; }
.cyc-reason { margin-top: 6px; padding: 8px 12px; background: #f8fafc; border-radius: 8px; font-size: 12px; color: #334155; font-weight: 500; line-height: 1.5; border-left: 3px solid #94a3b8; }
.cyc-rejects { margin-top: 8px; }
.cyc-reject-pill { display: inline-block; padding: 3px 9px; font-size: 10.5px; font-weight: 700; background: #fee2e2; color: #991b1b; border-radius: 5px; margin-right: 6px; margin-bottom: 4px; font-family: 'SF Mono', monospace; }
.cyc-expand { margin-top: 10px; }
.cyc-expand details { margin-top: 6px; }
.cyc-expand summary { cursor: pointer; color: #6b7280; font-weight: 600; font-size: 11.5px; padding: 4px 0; user-select: none; }
.cyc-expand summary:hover { color: #4f46e5; }
.cyc-expand pre { background: #fafaf6; padding: 12px 14px; border-radius: 8px; overflow-x: auto; font-size: 10.5px; line-height: 1.55; color: #1a1d24; box-shadow: 0 0 0 1px rgba(15,23,42,0.04); margin-top: 6px; max-height: 480px; }
.kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 6px 16px; margin: 6px 0; }
.kv-grid .k { font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 700; }
.kv-grid .v { font-size: 12.5px; color: #1a1d24; font-weight: 600; font-variant-numeric: tabular-nums; }
.tag-decision { display: inline-block; padding: 3px 8px; font-size: 11px; font-weight: 700; border-radius: 6px; letter-spacing: 0.01em; }
.tag-decision.long  { background: #d1fae5; color: #065f46; }
.tag-decision.short { background: #fee2e2; color: #b91c1c; }
.tag-decision.no    { background: #f3f4f6; color: #6b7280; }
.tag-sup   { display: inline-block; padding: 2px 6px; font-size: 10px; font-weight: 700; border-radius: 4px; letter-spacing: 0.01em; }
.tag-sup.keep    { background: #d1fae5; color: #065f46; }
.tag-sup.dn      { background: #fef3c7; color: #92400e; }
.tag-sup.veto    { background: #fee2e2; color: #b91c1c; }

.sys-pill { display: inline-block; padding: 3px 9px; font-size: 11px; font-weight: 700; border-radius: 6px; }
.sys-pill.ok { background: #d1fae5; color: #065f46; }
.sys-pill.warn { background: #fef3c7; color: #92400e; }
.sys-pill.danger { background: #fee2e2; color: #b91c1c; }

footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #f0eee7; font-size: 11.5px; color: #9ca3af; text-align: center; }
"""


# ─────────────────────────── render ───────────────────────────
def render(d: dict) -> str:
    rate = d["usdkrw"]
    eq = d["equity"]; pnl_usd = d["pnl_usd"]; pnl_pct = d["pnl_pct"]
    today_net = d["today_net"]; today_n = d["today"].get("n_trades", 0)
    pnl_cls = "pos" if pnl_usd > 0 else ("neg" if pnl_usd < 0 else "mut")
    pnl_arrow = "▲" if pnl_usd > 0 else ("▼" if pnl_usd < 0 else "·")
    today_cls = "pos" if today_net > 0 else ("neg" if today_net < 0 else "mut")

    pos = d.get("position", {}) or {}
    is_open = pos.get("status") == "OPEN"

    risk = d.get("risk", {}) or {}
    manual = int(risk.get("manual_kill", 0) or 0)
    streak = int(risk.get("loss_streak", 0) or 0)
    # cooldown / streak-based kill are disabled in the current design; only
    # manual_kill remains as a hard gate.
    if manual:
        risk_badge = '<span class="badge-bad">MANUAL KILL</span>'
    else:
        risk_badge = '<span class="badge-on">OK</span>'

    mem_cls = "ok" if d["mem_mi"] < 600 else ("warn" if d["mem_mi"] < 800 else "danger")

    parts: list[str] = []
    parts.append(f'<!doctype html><html><head><meta charset="utf-8"><title>METIS · {d["mode"]}</title>')
    parts.append(f'<style>{CSS}</style></head><body><div class="wrap">')

    # TOPBAR
    svc_v5 = '<span class="svc-dot svc-on"></span>' if d["svc"].get("metis-v5") else '<span class="svc-dot svc-off"></span>'
    # 'metis-v4-bot' is a legacy systemd unit still polled on the host; keep the literal id.
    svc_v4 = '<span class="svc-dot svc-on"></span>' if d["svc"].get("metis-v4-bot") else '<span class="svc-dot svc-off"></span>'
    mode_pill = "PAPER" if d["mode"] == "PAPER" else "LIVE"
    on = '<span class="svc-dot svc-on"></span>'
    off = '<span class="svc-dot svc-off"></span>'
    dot_v5 = on if d["svc"].get("metis-v5") else off
    dot_v4 = on if d["svc"].get("metis-v4-bot") else off
    parts.append(
        '<div class="topbar">'
        '<div class="brand">'
        '<div class="brand-mark">운영자</div>'
        '<div class="brand-divider"></div>'
        '<div class="brand-text">'
        f'<div class="brand-title">DELTA OMEGA <span class="mode">// {mode_pill}</span></div>'
        '<div class="brand-sub">SOL · ETH perp scalping · gemini-3.5-flash</div>'
        '</div>'
        '</div>'
        '<div class="topmeta">'
        f'<span><span class="lbl">engine</span>{dot_v5}</span>'
        f'<span><span class="lbl">trend</span>{dot_v4}</span>'
        f'<span><span class="lbl">fx</span><b>₩{rate:,.0f}</b></span>'
        f'<span><span class="lbl">mem</span><span class="sys-pill {mem_cls}">{d["mem_mi"]}Mi</span></span>'
        f'<span><span class="lbl">disk</span><b>{esc(d["disk"])}</b></span>'
        f'<span><span class="lbl">kst</span><b>{esc(d["now_kst"][11:19])}</b></span>'
        '</div>'
        '</div>'
    )

















































































































    # ─────────── CURRENT POSITION — 핵심, 가장 prominent ───────────
    parts.append('<div class="sec-h" style="margin-top:0;"><h2 style="font-size:24px;">🎯 현재 포지션</h2>'
                 '<span class="sec-tag">실시간 P&L</span></div>')
    if is_open:
        side = pos.get("side", "?")
        side_lbl = "LONG" if side == "Buy" else "SHORT"
        side_color = "#10b981" if side == "Buy" else "#dc2626"
        symbol = pos.get("symbol", "?")
        entry = float(pos.get("entry_price", 0) or 0)
        sl = float(pos.get("stop_loss", 0) or 0)
        tp = float(pos.get("take_profit", 0) or 0)
        qty = float(pos.get("qty", 0) or 0)
        lev = int(pos.get("leverage", 1) or 1)
        time_stop_min = int(pos.get("time_stop_minutes", 0) or 0)
        opened = pos.get("opened_utc") or ""
        setup_id = pos.get("setup_id", "?")
        conf = pos.get("confidence") or 0

        # 실시간 mark price (3s cache)
        mark = get_mark_price(symbol)
        if mark > 0 and entry > 0:
            if side == "Buy":
                price_move_pct = (mark - entry) / entry * 100
                pnl_usd = (mark - entry) * qty
            else:
                price_move_pct = (entry - mark) / entry * 100
                pnl_usd = (entry - mark) * qty
            sl_dist = abs(entry - sl) if sl > 0 else 0
            r_now = ((mark - entry) if side == "Buy" else (entry - mark)) / sl_dist if sl_dist > 0 else 0
            if side == "Buy":
                dist_sl_pct = (mark - sl) / mark * 100 if sl > 0 else 0
                dist_tp_pct = (tp - mark) / mark * 100 if tp > 0 else 0
            else:
                dist_sl_pct = (sl - mark) / mark * 100 if sl > 0 else 0
                dist_tp_pct = (mark - tp) / mark * 100 if tp > 0 else 0
        else:
            price_move_pct = 0; pnl_usd = 0; r_now = 0; dist_sl_pct = 0; dist_tp_pct = 0

        elapsed_min = 0; remaining_min = time_stop_min
        try:
            opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
            elapsed_min = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60.0
            remaining_min = max(0, time_stop_min - elapsed_min)
        except Exception:
            pass

        pnl_cls = "pos" if pnl_usd > 0 else ("neg" if pnl_usd < 0 else "mut")
        pnl_krw = pnl_usd * rate

        parts.append(
            f'<div class="card" style="border-top: 6px solid {side_color}; padding: 24px 28px;">'
            f'<div style="display: flex; align-items: center; gap: 18px; margin-bottom: 20px; flex-wrap: wrap;">'
            f'<div style="font-size: 28px; font-weight: 800; color: #0f172a; letter-spacing: -0.02em;">'
            f'{esc(symbol)}</div>'
            f'<span style="font-size: 16px; font-weight: 800; padding: 8px 18px; border-radius: 8px; '
            f'background: {side_color}; color: white;">{side_lbl}</span>'
            f'<div style="font-size: 14px; color: #4b5563;">setup <b>{esc(setup_id)}</b>  ·  '
            f'conf <b>{conf:.2f}</b>  ·  lev <b>{lev}x</b>  ·  qty <b>{qty}</b></div>'
            f'</div>'

            # BIG P&L HERO (가장 큰 정보)
            f'<div style="display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 30px; '
            f'padding: 22px 0; border-top: 1px solid #f0eee7; border-bottom: 1px solid #f0eee7;">'

            f'<div><div class="hero-l">💵 미실현 PnL</div>'
            f'<div class="hero-v {pnl_cls}" style="font-size: 38px;">${pnl_usd:+,.3f}</div>'
            f'<div class="hero-krw" style="font-size: 16px; font-weight: 700;" class="{pnl_cls}">'
            f'<span class="{pnl_cls}">₩{pnl_krw:+,.0f}</span></div></div>'

            f'<div><div class="hero-l">현재가</div>'
            f'<div class="hero-v small">${mark:,.4f}</div>'
            f'<div class="hero-sub"><span class="{pnl_cls}">{price_move_pct:+.3f}%</span> from entry</div></div>'

            f'<div><div class="hero-l">R-multiple</div>'
            f'<div class="hero-v small {pnl_cls}">{r_now:+.2f}R</div>'
            f'<div class="hero-sub">target <b>{(pos.get("target_R") or 0):.2f}R</b></div></div>'

            f'<div><div class="hero-l">⏱ 보유</div>'
            f'<div class="hero-v small">{elapsed_min:.0f}m</div>'
            f'<div class="hero-sub">SL/TP only · 시간 무관</div></div>'

            f'</div>'

            # entry / SL / TP details
            f'<div class="mini-grid" style="margin-top: 18px;">'
            f'<div><span class="mlbl">Entry</span><b>${entry:,.4f}</b></div>'
            f'<div><span class="mlbl">Stop Loss</span><b>${sl:,.4f}</b> '
            f'<span class="mut">({dist_sl_pct:+.2f}%)</span></div>'
            f'<div><span class="mlbl">Take Profit</span><b>${tp:,.4f}</b> '
            f'<span class="mut">({dist_tp_pct:+.2f}%)</span></div>'
            f'<div><span class="mlbl">Notional</span><b>${qty*entry:,.2f}</b></div>'
            f'<div><span class="mlbl">Opened (UTC)</span><b>{esc(opened[:19])}</b></div>'
            f'</div></div>'
        )
    else:
        parts.append(
            '<div class="card" style="border-top: 6px solid #d1d5db; padding: 28px;">'
            '<div style="display: flex; align-items: center; gap: 14px;">'
            '<div style="font-size: 22px; font-weight: 700; color: #6b7280;">⏸ 포지션 없음</div>'
            '<div class="card-sub" style="font-size: 14px;">A급 setup 대기 중 — 다음 15분 cycle</div>'
            '</div></div>'
        )

    # HERO — equity + today + drawdown + risk
    parts.append('<div class="hero"><div class="hero-grid">')
    parts.append(
        '<div class="hero-cell">'
        '<div class="hero-l">총 자산 (PAPER)</div>'
        f'<div class="hero-v">${eq:,.2f}</div>'
        f'<div class="hero-sub"><span class="{pnl_cls}">{pnl_arrow} ${abs(pnl_usd):,.2f} ({pnl_pct:+.2f}%)</span> · 원금 ${d["initial"]:,.0f}</div>'
        f'<div class="hero-krw">{krw_str(eq, rate)} · 손익 {krw_str(pnl_usd, rate)}</div>'
        '</div>'
    )
    parts.append(
        '<div class="hero-cell">'
        '<div class="hero-l">오늘 P&L</div>'
        f'<div class="hero-v {today_cls} small">${today_net:+.2f}</div>'
        f'<div class="hero-sub">{today_n} trades · W {d["today"].get("n_wins", 0)} L {d["today"].get("n_losses", 0)}</div>'
        f'<div class="hero-krw">{krw_str(today_net, rate)}</div>'
        '</div>'
    )
    parts.append(
        '<div class="hero-cell">'
        '<div class="hero-l">Drawdown</div>'
        f'<div class="hero-v small">{d["dd_pct"]:.2f}%</div>'
        f'<div class="hero-sub">HW ${d["high_water"]:,.2f}</div>'
        f'<div class="hero-krw">streak <b>{streak}</b></div>'
        '</div>'
    )
    parts.append(
        '<div class="hero-cell">'
        '<div class="hero-l">Risk State</div>'
        f'<div class="hero-v small">{risk_badge}</div>'
        f'<div class="hero-sub">reason: {esc((risk.get("reason") or "-")[:60])}</div>'
        f'<div class="hero-krw">updated: {esc((risk.get("updated_utc") or "-")[:19])}</div>'
        '</div>'
    )
    parts.append('</div></div>')

    # AI 판단 로그 (50건) — feature_snapshot 펼쳐보기
    parts.append('<div class="sec-h"><h2>AI 판단 로그</h2><span class="sec-tag">최근 50 cycles</span></div>')
    if not d["cycles"]:
        parts.append('<div class="card"><div class="muted">아직 cycle 실행 안 됨 — 15분 boundary 기다리는 중</div></div>')
    else:
        for c in d["cycles"]:
            decision = c.get("decision") or "?"
            sym = c.get("symbol") or "?"
            ts = (c.get("asof_utc") or "")[11:19]
            conf = c.get("confidence")
            setup = c.get("setup_id") or "-"
            regime = c.get("regime") or "-"
            vol = c.get("volatility_regime") or "-"
            sup = c.get("supervisor_action") or "-"
            rejects = c.get("critical_rejects") or ""
            risk_pass = c.get("risk_pass", 0)
            veto = c.get("risk_veto_reason") or ""
            lat = c.get("llm_latency_ms")
            cache = c.get("cache_hit", 0)
            tok_in = c.get("llm_input_tokens", 0)
            tok_out = c.get("llm_output_tokens", 0)

            # Determine card class + decision tag
            if decision == "ENTER_LONG":
                card_cls = "long"; dec_lbl = "LONG"; dec_tag_cls = "long"
            elif decision == "ENTER_SHORT":
                card_cls = "short"; dec_lbl = "SHORT"; dec_tag_cls = "short"
            elif veto:
                card_cls = "veto"; dec_lbl = "VETO"; dec_tag_cls = "veto"
            else:
                card_cls = "no"; dec_lbl = "NO TRADE"; dec_tag_cls = "no"

            # Parse llm_full_json
            tech_ev = supervisor_ev = reason = ""
            llm_json_pretty = ""
            ai_leverage = None
            try:
                if c.get("llm_full_json"):
                    parsed = json.loads(c["llm_full_json"])
                    tech_ev = (parsed.get("technical_proposal") or {}).get("technical_evidence_summary", "") or ""
                    supervisor_ev = (parsed.get("supervisor_review") or {}).get("supervisor_opposing_evidence", "") or ""
                    reason = parsed.get("reason_short", "") or ""
                    ai_leverage = parsed.get("leverage")
                    llm_json_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass

            feature_pretty = ""
            try:
                if c.get("feature_snapshot_json"):
                    parsed = json.loads(c["feature_snapshot_json"])
                    feature_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                feature_pretty = c.get("feature_snapshot_json") or ""

            controls_pretty = ""
            try:
                if c.get("runtime_controls_json"):
                    parsed = json.loads(c["runtime_controls_json"])
                    controls_pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                controls_pretty = c.get("runtime_controls_json") or ""

            # ─── Card ───
            parts.append(f'<div class="cyc-card {card_cls}">')

            # Head row — ts | symbol | decision + meta | right meta
            sup_chip = ""
            if sup and sup != "-":
                sup_chip = f'<span class="chip">supervisor: <b>{esc(sup)}</b></span>'
            chips = []
            if setup and setup != "-":
                chips.append(f'<span class="chip">setup: <b>{esc(setup)}</b></span>')
            if conf is not None:
                chips.append(f'<span class="chip">conf: <b>{conf:.2f}</b></span>')
            if ai_leverage:
                chips.append(f'<span class="chip">lev: <b>{ai_leverage}x</b></span>')
            chips.append(f'<span class="chip">regime: <b>{esc(regime)}</b>/<b>{esc(vol)}</b></span>')
            if sup_chip:
                chips.append(sup_chip)
            chips_html = "".join(chips)

            parts.append(
                f'<div class="cyc-head">'
                f'<div class="cyc-ts">{ts}</div>'
                f'<div class="cyc-sym">{esc(sym)}</div>'
                f'<div class="cyc-meta-mid">'
                f'<span class="cyc-tag {dec_tag_cls}">{dec_lbl}</span>'
                f'{chips_html}'
                f'</div>'
                f'<div class="cyc-meta-right">{(lat or 0)/1000:.1f}s<br>'
                f'{tok_in}↑/{tok_out}↓ · {"✓cache" if cache else "✗cache"}</div>'
                f'</div>'
            )

            # Body — evidence always visible
            body_has_content = False
            body_parts = ['<div class="cyc-body">']
            if tech_ev or supervisor_ev:
                if tech_ev and supervisor_ev:
                    body_parts.append('<div class="cyc-evidence">')
                else:
                    body_parts.append('<div class="cyc-evidence single">')
                if tech_ev:
                    body_parts.append(
                        f'<div class="cyc-ev tech">'
                        f'<div class="lbl">📊 Technical evidence</div>'
                        f'<div class="val">{esc(tech_ev)}</div></div>'
                    )
                if supervisor_ev:
                    body_parts.append(
                        f'<div class="cyc-ev sup">'
                        f'<div class="lbl">🛡 Supervisor opposing</div>'
                        f'<div class="val">{esc(supervisor_ev)}</div></div>'
                    )
                body_parts.append('</div>')
                body_has_content = True

            if veto:
                body_parts.append(
                    f'<div class="cyc-ev veto" style="margin-bottom: 8px;">'
                    f'<div class="lbl">⚠️ Risk engine veto</div>'
                    f'<div class="val">{esc(veto)}</div></div>'
                )
                body_has_content = True

            if reason:
                body_parts.append(f'<div class="cyc-reason">💬 {esc(reason)}</div>')
                body_has_content = True

            if rejects:
                reject_pills = "".join(
                    f'<span class="cyc-reject-pill">{esc(r.strip())}</span>'
                    for r in rejects.split(",") if r.strip()
                )
                body_parts.append(f'<div class="cyc-rejects">{reject_pills}</div>')
                body_has_content = True

            # Collapsible details (input features / runtime controls / full json)
            if feature_pretty or controls_pretty or llm_json_pretty:
                body_parts.append('<div class="cyc-expand">')
                if feature_pretty:
                    body_parts.append(
                        f'<details><summary>📥 Input features ({len(feature_pretty):,} chars)</summary>'
                        f'<pre>{esc(feature_pretty)}</pre></details>'
                    )
                if controls_pretty:
                    body_parts.append(
                        f'<details><summary>⚙️ Runtime controls</summary>'
                        f'<pre>{esc(controls_pretty)}</pre></details>'
                    )
                if llm_json_pretty:
                    body_parts.append(
                        f'<details><summary>🤖 Full LLM JSON</summary>'
                        f'<pre>{esc(llm_json_pretty)}</pre></details>'
                    )
                body_parts.append('</div>')
                body_has_content = True

            body_parts.append('</div>')
            if body_has_content:
                parts.extend(body_parts)

            parts.append('</div>')  # /cyc-card

    # Outcomes
    parts.append('<div class="sec-h"><h2>최근 청산</h2><span class="sec-tag">closed trades</span></div>')
    if d["outcomes"]:
        parts.append('<div class="card"><table>')
        parts.append('<thead><tr><th>closed</th><th>sym</th><th>side</th><th>setup</th>'
                    '<th>entry→exit</th><th>R</th><th>USD</th><th>KRW</th><th>fees</th>'
                    '<th>reason</th><th>win</th></tr></thead><tbody>')
        for o in d["outcomes"]:
            pnl = float(o.get("realized_pnl_usdt") or 0)
            pnl_cls = "pos" if pnl > 0 else "neg"
            R = float(o.get("realized_R") or 0)
            won = "✓" if o.get("won") else "✗"
            won_cls = "pos" if o.get("won") else "neg"
            parts.append(
                f'<tr><td class="mono">{esc((o.get("closed_utc") or "")[5:19])}</td>'
                f'<td>{esc(o.get("symbol"))}</td>'
                f'<td>{esc(o.get("side"))}</td>'
                f'<td>{esc(o.get("setup_id"))}</td>'
                f'<td class="mono">{(o.get("entry_price") or 0):.4f} → {(o.get("exit_price") or 0):.4f}</td>'
                f'<td class="{pnl_cls}">{R:+.2f}</td>'
                f'<td class="{pnl_cls}">${pnl:+.2f}</td>'
                f'<td class="{pnl_cls}">{krw_str(pnl, rate)}</td>'
                f'<td>{(o.get("fees_usdt") or 0):.3f}</td>'
                f'<td>{esc(o.get("close_reason"))}</td>'
                f'<td class="{won_cls}"><b>{won}</b></td>'
                f'</tr>'
            )
        parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="card"><div class="muted">청산된 거래 없음</div></div>')

    # Setup stats
    parts.append('<div class="sec-h"><h2>Setup 통계</h2><span class="sec-tag">all-time</span></div>')
    if d["setup_stats"]:
        parts.append('<div class="card"><table>')
        parts.append('<thead><tr><th>setup</th><th>n</th><th>WR%</th><th>avg R</th>'
                    '<th>avg PnL</th><th>sum PnL</th><th>sum KRW</th><th>avg min</th></tr></thead><tbody>')
        for s in d["setup_stats"]:
            n = s["n"] or 0
            wins = s["wins"] or 0
            wr = (wins / n * 100) if n else 0
            sum_pnl = float(s["sum_pnl"] or 0)
            sp_cls = "pos" if sum_pnl > 0 else ("neg" if sum_pnl < 0 else "mut")
            parts.append(
                f'<tr><td><b>{esc(s["setup_id"])}</b></td>'
                f'<td>{n}</td><td>{wr:.1f}</td>'
                f'<td>{(s["avg_R"] or 0):+.2f}</td>'
                f'<td>{(s["avg_pnl"] or 0):+.2f}</td>'
                f'<td class="{sp_cls}">{sum_pnl:+.2f}</td>'
                f'<td class="{sp_cls}">{krw_str(sum_pnl, rate)}</td>'
                f'<td>{(s["avg_min"] or 0):.0f}</td></tr>'
            )
        parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="card"><div class="muted">아직 통계 없음 (closed trade 0)</div></div>')

    # Confidence calibration
    parts.append('<div class="sec-h"><h2>Confidence buckets</h2><span class="sec-tag">all cycles</span></div>')
    if d["conf_buckets"]:
        parts.append('<div class="card"><table>')
        parts.append('<thead><tr><th>bucket</th><th>cycles</th><th>entered</th><th>enter %</th></tr></thead><tbody>')
        for b in d["conf_buckets"]:
            n = b["n"] or 0
            ent = b["entered"] or 0
            rate_pct = (ent / n * 100) if n else 0
            parts.append(f'<tr><td>{esc(b["bucket"])}</td><td>{n}</td><td>{ent}</td><td>{rate_pct:.1f}</td></tr>')
        parts.append('</tbody></table></div>')
    else:
        parts.append('<div class="card"><div class="muted">데이터 모이는 중</div></div>')

    # System health
    parts.append('<div class="sec-h"><h2>시스템 상태</h2><span class="sec-tag">systemd · monitors</span></div>')
    ws = d.get("ws_health") or {}
    ts = d.get("time_sync") or {}
    hl = d.get("health") or {}
    ws_stale = bool(ws.get("any_stale"))
    ws_badge = '<span class="sys-pill danger">STALE</span>' if ws_stale else '<span class="sys-pill ok">OK</span>'
    ts_ok = ts.get("ok", True)
    ts_badge = ('<span class="sys-pill ok">OK</span>' if ts_ok
                else '<span class="sys-pill danger">DRIFT</span>')
    ts_off = ts.get("offset_ms")
    ts_off_str = f"{ts_off}ms" if ts_off is not None else "-"

    def _svc_badge(active: bool) -> str:
        return '<span class="badge-on">ACTIVE</span>' if active else '<span class="badge-off">INACTIVE</span>'

    parts.append('<div class="card"><div class="mini-grid">')
    parts.append(f'<div><span class="mlbl">engine</span><b>{_svc_badge(d["svc"].get("metis-v5"))}</b></div>')
    parts.append(f'<div><span class="mlbl">dashboard</span><b>{_svc_badge(d["svc"].get("metis-v5-dashboard"))}</b></div>')
    parts.append(f'<div><span class="mlbl">trend bot</span><b>{_svc_badge(d["svc"].get("metis-v4-bot"))}</b></div>')
    parts.append(f'<div><span class="mlbl">WS</span>{ws_badge}</div>')
    parts.append(f'<div><span class="mlbl">NTP</span>{ts_badge} <span class="mut">{ts_off_str}</span></div>')
    parts.append(f'<div><span class="mlbl">MEM RSS</span><b>{(hl.get("mem_rss_mb") or 0):.0f} MB</b></div>')
    parts.append(f'<div><span class="mlbl">CPU%</span><b>{(hl.get("cpu_pct") or 0):.0f}</b></div>')
    parts.append(f'<div><span class="mlbl">Disk free</span><b>{(hl.get("disk_free_pct") or 1.0)*100:.1f}%</b></div>')
    parts.append('</div></div>')

    parts.append(f'<footer>METIS · paper {esc(d["mode"])} · '
                 f'symbols {esc(", ".join(d["symbols"]))} · USD/KRW {rate:,.0f} · '
                 f'bare http.server · F5 refresh</footer>')
    parts.append('</div></body></html>')
    return "".join(parts)


# ─────────────────────────── HTTP server ───────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        try:
            d = gather()
            body = render(d).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(f"Error: {e}".encode())

    def log_message(self, *a):
        pass


def main():
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    addr = os.getenv("DASHBOARD_ADDR", "0.0.0.0")
    print(f"METIS dashboard listening on {addr}:{port}")
    HTTPServer((addr, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
