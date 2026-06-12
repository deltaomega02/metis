#!/usr/bin/env python3
"""BTC 추세봇 상태 + 실시간 시장/지갑을 한 번에 덤프하는 진단 스크립트."""
import sqlite3, os, datetime
from pybit.unified_trading import HTTP

DB = "/home/botuser/metis-v4/metis_v4.db"
s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))

# DB에서 봇 메타/상태/거래 이력 적재
c = sqlite3.connect(DB)
meta = dict(c.execute("select k,val from meta").fetchall())
state = c.execute("select position,entry_price,btc_qty,last_acted_date,last_heartbeat from state where id=1").fetchone()
live = c.execute("select ts,usdt,btc,price,equity,dist_pct,position from live where id=1").fetchone()
trades = c.execute("select ts,action,price,usdt,btc_qty,ret_pct,equity,mode from trades order by id").fetchall()
eqlog = c.execute("select date,price,sma125,dist_pct,equity from equity_log order by ts").fetchall()
c.close()

# 시장 — 완성 일봉만 사용해 SMA125 계산
r = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=200)
rows = sorted(([int(k[0]), float(k[4])] for k in r["result"]["list"]), key=lambda x: x[0])
today0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
rows = [x for x in rows if x[0] < today0]
closes = [x[1] for x in rows]
sma = sum(closes[-125:]) / 125
lc = closes[-1]
t = s.get_tickers(category="spot", symbol="BTCUSDT")["result"]["list"][0]
px = float(t["lastPrice"]); chg = float(t.get("price24hPcnt", 0)) * 100
bal = {x["coin"]: float(x.get("walletBalance", 0) or 0)
       for x in s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]}
bal = {k: v for k, v in bal.items() if v > 0}

# 손익 집계
init = float(meta.get("initial_equity", 0) or 0)
usdt = bal.get("USDT", 0.0); btc = bal.get("BTC", 0.0)
equity = usdt + btc * px
profit = equity - init
pct = (profit / init * 100) if init else 0
start = meta.get("start_ts")
days = ((datetime.datetime.utcnow() - datetime.datetime.fromisoformat(start)).total_seconds() / 86400) if start else None
entry = state[1] if state else None
upnl = ((px / entry - 1) * 100) if entry else 0

print(f"META: {meta}")
print(f"STATE: {state}")
print(f"LIVE_DB: {live}")
print(f"WALLET: usdt={usdt:.2f} btc={btc:.8f} equity=${equity:.2f}")
print(f"PROFIT: baseline ${init:.2f} | profit ${profit:+.2f} ({pct:+.2f}%) | days={days:.2f}" if days else f"PROFIT: ${profit:+.2f}")
print(f"POSITION: {state[0] if state else '?'} entry=${entry:,.0f} upnl={upnl:+.2f}%" if entry else f"POSITION: {state[0] if state else '?'}")
print(f"MARKET: lastclose=${lc:,.0f} | sma125=${sma:,.0f} | px=${px:,.0f} | 24h={chg:+.1f}% | close_vs_sma={(lc/sma-1)*100:+.1f}%")
print(f"EXIT_LINE: sma125*0.98=${sma*0.98:,.0f} | px_to_exit={(sma*0.98/px-1)*100:+.1f}%")
print("TRADES:")
for tr in trades:
    print(f"   {tr}")
print(f"EQLOG ({len(eqlog)} rows):")
for e in eqlog:
    print(f"   {e}")
