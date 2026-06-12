#!/usr/bin/env python3
"""
BTC 추세봇 — BTC 현물 SMA125 추세추종 (룰 기반, 무차입, 롱only).

매일 한 번(UTC 일봉 마감 기준) 다음 규칙으로 의사결정:
  현금 보유 + 종가 > SMA125          → 전액 BTC 매수 (한도 METIS_V4_ALLOC)
  BTC 보유 + 종가 < SMA125 × 0.98    → 전량 매도하여 현금화
  그 외                                → 유지

안전장치:
  - METIS_V4_LIVE=1 일 때만 실주문. 미설정/0 이면 DRY (로그·텔레그램만, 주문 없음).
  - 현물 주문 (category=spot). 매수는 quoteCoin(USDT 금액), 매도는 baseCoin(BTC 수량).
  - isLeverage=0 → 레버리지 차입 절대 사용 안 함.

env: BYBIT_API_KEY, BYBIT_API_SECRET(또는 BYBIT_SECRET), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     METIS_V4_ALLOC(기본 950), METIS_V4_LIVE(기본 0), METIS_V4_INTERVAL(초, 기본 3600)
"""
import os, sys, time, datetime, logging, sqlite3
import requests
from pybit.unified_trading import HTTP

SYMBOL = "BTCUSDT"
SMA_N = 125
EXIT_BUFFER = 0.02                                    # 청산선 = SMA125 × (1-EXIT_BUFFER). 추세 깨짐 확정 후 매도
ALLOC = float(os.getenv("METIS_V4_ALLOC", "950"))     # 한 번 매수에 투입할 최대 USDT
LIVE = os.getenv("METIS_V4_LIVE", "0") == "1"
CHECK = int(os.getenv("METIS_V4_INTERVAL", "3600"))   # 사이클 주기(초). 일봉 기준이지만 시간 단위로 깨워 마감 즉시 반응
DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "metis_v4.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("metis_v4")

API_KEY = os.getenv("BYBIT_API_KEY")
API_SEC = os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET") or os.getenv("BYBIT_API_SECRET_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
MTAG = "🔴LIVE" if LIVE else "🟡DRY"                  # 텔레그램 메시지 앞에 붙이는 모드 표시

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SEC)


def tg(msg):
    """텔레그램 알림 전송 (실패해도 봇은 계속 동작)."""
    log.info("TG: " + msg.replace("\n", " | "))
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception as e:
        log.error(f"telegram 실패: {e}")


# ---------- DB ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """스키마 보장 + state 행이 없으면 초기 상태(현금)로 생성."""
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS state(
        id INTEGER PRIMARY KEY CHECK(id=1), position TEXT, entry_price REAL,
        btc_qty REAL, last_acted_date TEXT, last_heartbeat TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, price REAL,
        usdt REAL, btc_qty REAL, ret_pct REAL, equity REAL, mode TEXT);
    CREATE TABLE IF NOT EXISTS equity_log(
        ts TEXT, date TEXT, position TEXT, price REAL, sma125 REAL,
        dist_pct REAL, usdt REAL, btc REAL, equity REAL);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, val TEXT);
    CREATE TABLE IF NOT EXISTS live(
        id INTEGER PRIMARY KEY CHECK(id=1), ts TEXT, usdt REAL, btc REAL,
        price REAL, equity REAL, sma125 REAL, dist_pct REAL, position TEXT);
    """)
    c.execute("INSERT OR IGNORE INTO state(id,position,entry_price,btc_qty,last_acted_date,last_heartbeat,updated_at) "
              "VALUES(1,'CASH',NULL,0,NULL,NULL,?)", (datetime.datetime.utcnow().isoformat(),))
    c.commit(); c.close()


def get_state():
    c = db(); r = dict(c.execute("SELECT * FROM state WHERE id=1").fetchone()); c.close(); return r


def set_state(**kw):
    kw["updated_at"] = datetime.datetime.utcnow().isoformat()
    cols = ",".join(f"{k}=?" for k in kw)
    c = db(); c.execute(f"UPDATE state SET {cols} WHERE id=1", tuple(kw.values())); c.commit(); c.close()


def log_trade(mode, action, p, usdt, btc, ret, equity):
    c = db(); c.execute("INSERT INTO trades(ts,action,price,usdt,btc_qty,ret_pct,equity,mode) VALUES(?,?,?,?,?,?,?,?)",
                        (datetime.datetime.utcnow().isoformat(), action, p, usdt, btc, ret, equity, mode))
    c.commit(); c.close()


def log_equity(date, pos, p, sma, dist, usdt, btc, equity):
    c = db(); c.execute("INSERT INTO equity_log(ts,date,position,price,sma125,dist_pct,usdt,btc,equity) VALUES(?,?,?,?,?,?,?,?,?)",
                        (datetime.datetime.utcnow().isoformat(), date, pos, p, sma, dist, usdt, btc, equity))
    c.commit(); c.close()


def meta_get(k):
    c = db(); r = c.execute("SELECT val FROM meta WHERE k=?", (k,)).fetchone(); c.close()
    return r[0] if r else None


def meta_set(k, v):
    c = db(); c.execute("INSERT OR REPLACE INTO meta(k,val) VALUES(?,?)", (k, str(v))); c.commit(); c.close()


def upsert_live(usdt, btc, p, equity, sma, dist, pos):
    """대시보드용 실시간 스냅샷(단일 행)."""
    c = db(); c.execute(
        "INSERT OR REPLACE INTO live(id,ts,usdt,btc,price,equity,sma125,dist_pct,position) VALUES(1,?,?,?,?,?,?,?,?)",
        (datetime.datetime.utcnow().isoformat(), usdt, btc, p, equity, sma, dist, pos))
    c.commit(); c.close()


# ---------- 시세 / 잔고 ----------
def daily_closes():
    """완성된 일봉만 반환. 오늘 진행 중인 봉은 제외해야 SMA가 흔들리지 않음."""
    r = session.get_kline(category="spot", symbol=SYMBOL, interval="D", limit=200)
    rows = sorted(([int(k[0]), float(k[4])] for k in r["result"]["list"]), key=lambda x: x[0])
    today0 = int(datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    return [x for x in rows if x[0] < today0]


def wallet():
    r = session.get_wallet_balance(accountType="UNIFIED")
    return {c["coin"]: float(c.get("walletBalance", 0) or 0) for c in r["result"]["list"][0]["coin"]}


def price():
    return float(session.get_tickers(category="spot", symbol=SYMBOL)["result"]["list"][0]["lastPrice"])


def equity_now():
    b = wallet(); return b.get("USDT", 0.0) + b.get("BTC", 0.0) * price()


# ---------- 주문 ----------
def spot_buy(usdt):
    """USDT 금액으로 BTC 시장가 매수 (quoteCoin 방식)."""
    return session.place_order(category="spot", symbol=SYMBOL, side="Buy", orderType="Market",
                               qty=str(round(usdt, 2)), marketUnit="quoteCoin", isLeverage=0)


def spot_sell(btc):
    """BTC 수량으로 시장가 매도 (baseCoin 방식)."""
    return session.place_order(category="spot", symbol=SYMBOL, side="Sell", orderType="Market",
                               qty=str(btc), marketUnit="baseCoin", isLeverage=0)


# ---------- 1 사이클 ----------
def run_once():
    """일봉 마감 기준으로 매수/매도/유지 판단. 하루 1회만 실제 행동(last_acted_date 가드)."""
    rows = daily_closes()
    closes = [r[1] for r in rows]
    last_close = closes[-1]
    last_date = datetime.datetime.utcfromtimestamp(rows[-1][0] / 1000).strftime("%Y-%m-%d")
    sma = sum(closes[-SMA_N:]) / SMA_N
    dist = (last_close / sma - 1) * 100
    st = get_state(); pos = st["position"]
    bal = wallet(); usdt = bal.get("USDT", 0.0); btc = bal.get("BTC", 0.0)
    cur = price(); equity = usdt + btc * cur
    mode = "LIVE" if LIVE else "DRY"

    # 신호 판정
    if pos == "CASH":
        action = "BUY" if last_close > sma else "STAY"
    else:
        action = "SELL" if last_close < sma * (1 - EXIT_BUFFER) else "HOLD"

    acted = False
    # 같은 날짜에 이미 행동했으면 중복 실행 방지
    if action in ("BUY", "SELL") and st.get("last_acted_date") != last_date:
        if action == "BUY":
            # 0.3% 여유는 수수료/슬리피지 버퍼. ALLOC 한도 적용.
            amt = min(ALLOC, usdt * 0.997)
            if amt >= 5:
                if LIVE:
                    spot_buy(amt); time.sleep(3); btc = wallet().get("BTC", 0.0); equity = equity_now()
                else:
                    btc = round(amt / cur, 6); equity = (usdt - amt) + btc * cur
                set_state(position="LONG", entry_price=cur, btc_qty=btc, last_acted_date=last_date)
                log_trade(mode, "BUY", cur, amt, btc, None, equity)
                acted = True
                tg(f"{MTAG} 🟢 BUY 체결\nBTC ${cur:,.0f} 매수 (${amt:,.0f})\nSMA125 ${sma:,.0f} (종가 {dist:+.1f}%)\n자산 ≈ ${equity:,.0f}")
        elif action == "SELL" and (btc > 0 or not LIVE):
            qty = round(btc, 6) if LIVE else round(st.get("btc_qty") or 0, 6)
            if LIVE and qty > 0:
                spot_sell(qty); time.sleep(3); equity = equity_now()
            ret = (cur / st["entry_price"] - 1) * 100 if st.get("entry_price") else 0
            set_state(position="CASH", entry_price=None, btc_qty=0, last_acted_date=last_date)
            log_trade(mode, "SELL", cur, qty * cur, qty, ret, equity)
            acted = True
            tg(f"{MTAG} 🔴 SELL 체결\nBTC ${cur:,.0f} 전량 매도 ({qty} BTC, {ret:+.1f}%)\nSMA125 ${sma:,.0f} (종가 {dist:+.1f}%)\n자산 ≈ ${equity:,.0f}")

    # 하루 1회 heartbeat 리포트 + equity 로그 적재
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    st = get_state()
    if st.get("last_heartbeat") != today:
        log_equity(last_date, st["position"], cur, sma, dist, usdt, btc, equity)
        if not acted:
            if st["position"] == "LONG" and st.get("entry_price"):
                pos_txt = f"BTC 보유 (진입 ${st['entry_price']:,.0f}, {(cur/st['entry_price']-1)*100:+.1f}%)"
            else:
                pos_txt = "현금 대기"
            sig = {"BUY": "매수 집행 예정", "SELL": "매도 신호", "HOLD": "보유 유지", "STAY": "현금 유지(추세 아래)"}[action]
            tg(f"{MTAG} 📊 일일 리포트 {today}\n상태: {pos_txt}\nBTC ${cur:,.0f} / SMA125 ${sma:,.0f} (종가 {dist:+.1f}%)\n판단: {sig}\n자산 ≈ ${equity:,.0f}")
        set_state(last_heartbeat=today)

    # 대시보드 스냅샷 — 거래 직후 정확한 잔고를 다시 읽어 저장
    b2 = wallet(); u2 = b2.get("USDT", 0.0); c2 = b2.get("BTC", 0.0); eq2 = u2 + c2 * cur
    upsert_live(u2, c2, cur, eq2, sma, dist, get_state()["position"])

    log.info(f"{mode} close={last_close:.0f} sma={sma:.0f} dist={dist:+.1f}% pos={get_state()['position']} action={action} acted={acted} eq=${eq2:.0f}")


def main():
    once = "--once" in sys.argv
    init_db()
    # 최초 실행 시 원금/시작시각 고정. 누적 수익률 계산의 기준점.
    if meta_get("initial_equity") is None:
        meta_set("initial_equity", round(equity_now(), 2))
        meta_set("start_ts", datetime.datetime.utcnow().isoformat())
    tg(f"{MTAG} BTC 추세봇 시작 (BTC 현물 SMA125, 투입한도 ${ALLOC:,.0f})")
    while True:
        try:
            run_once()
        except Exception as e:
            log.exception("run_once 오류")
            tg(f"⚠️ BTC 추세봇 오류: {str(e)[:200]}")
        if once:
            break
        time.sleep(CHECK)


if __name__ == "__main__":
    main()
