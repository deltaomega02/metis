#!/usr/bin/env python3
"""
Carry bot — funding 수취용 델타중립 봇.

핵심 아이디어: 보유 중인 BTC의 일부에 대해 perp 숏을 잡아 가격 노출을 0으로 만들고,
숏 포지션이 지급받는 펀딩 수수료만 누적 수익으로 가져간다. USDT를 빌리지 않고
*보유 BTC 자체*가 숏의 담보가 되므로 빚이 발생하지 않는다(1x 무차입).

안전 규칙 (절대 위반 금지):
  - 숏 크기 ≤ 보유 BTC. naked 숏 금지. 추세봇이 BTC를 팔면 다음 사이클에 숏도 자동 축소.
  - 라이브 주문 직전 borrowAmount > 0 이면 즉시 중단 (안전 가드).
  - METIS_CARRY_LIVE=1 일 때만 실주문. 기본은 DRY(로그·텔레그램만).

신호:
  - 진입: 최근 3일 평균 펀딩 > FUND_THRESHOLD (0.06%/일)
  - 청산: 펀딩 ≤ 0 (kill-switch)

env: BYBIT_API_KEY/SECRET, TELEGRAM_*, METIS_CARRY_ALLOC(carry할당 USDT, 기본 460), METIS_CARRY_LIVE
"""
import os, sys, time, datetime, logging, sqlite3
import requests
from pybit.unified_trading import HTTP

SYMBOL = "BTCUSDT"
FUND_THRESHOLD = 0.0006        # 일평균 0.06% 이상이면 carry할 가치 있다고 판단
FUND_WINDOW = 3                # 최근 3일 펀딩 평균을 신호로 사용 (단발 스파이크 회피)
CARRY_ALLOC = float(os.getenv("METIS_CARRY_ALLOC", "460"))
LIVE = os.getenv("METIS_CARRY_LIVE", "0") == "1"
CHECK = int(os.getenv("METIS_CARRY_INTERVAL", "3600"))
DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "metis_v5_carry.db")
MTAG = "🔴LIVE" if LIVE else "🟡DRY"
QTY_STEP = 0.001               # BTCUSDT perp 최소 수량 단위. 이보다 작은 차이는 무시.

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("carry")
API_KEY = os.getenv("BYBIT_API_KEY")
API_SEC = os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET") or os.getenv("BYBIT_API_SECRET_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN"); TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SEC)


def tg(m):
    log.info("TG: " + m.replace("\n", " | "))
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": m}, timeout=10)
        except Exception as e: log.error(f"tg {e}")


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
def init_db():
    c = db(); c.executescript("""
      CREATE TABLE IF NOT EXISTS carry_log(ts TEXT, funding REAL, basis REAL, short_btc REAL, btc_held REAL, action TEXT, note TEXT);
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, val TEXT);
    """); c.commit(); c.close()
def log_row(funding, basis, short, btc, action, note=""):
    c = db(); c.execute("INSERT INTO carry_log VALUES(?,?,?,?,?,?,?)",
                        (datetime.datetime.utcnow().isoformat(), funding, basis, short, btc, action, note)); c.commit(); c.close()


def trailing_funding():
    """최근 3일치 펀딩(8h 단위 × 9건)을 평균낸 후 일 단위로 환산."""
    r = session.get_funding_rate_history(category="linear", symbol=SYMBOL, limit=FUND_WINDOW * 3 + 3)
    rates = [float(x["fundingRate"]) for x in r["result"]["list"]]
    recent = rates[:FUND_WINDOW * 3]
    return (sum(recent) / len(recent) * 3) if recent else 0.0   # 8h평균 × 3 = 일평균


def price(cat):
    return float(session.get_tickers(category=cat, symbol=SYMBOL)["result"]["list"][0]["lastPrice"])
def perp_short_size():
    for p in session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]:
        if p.get("side") == "Sell":
            return float(p.get("size", 0) or 0)
    return 0.0
def btc_holdings():
    for c in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]:
        if c["coin"] == "BTC":
            return float(c.get("walletBalance", 0) or 0)
    return 0.0
def borrow_total():
    """차입 합계. 무차입 원칙상 항상 0이어야 함. 안전 가드용."""
    tot = 0.0
    for c in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]:
        tot += float(c.get("borrowAmount", 0) or 0)
    return tot


def short_order(side, qty):
    """side=Sell이면 숏 진입/증대, side=Buy이면 숏 축소(reduceOnly로 안전)."""
    if qty < QTY_STEP:
        return
    if not LIVE:
        log.info(f"[DRY] perp {side} {qty} BTC (실주문 X)"); return
    # 안전 가드: 라이브 주문 직전 차입이 0이 아니면 중단
    if borrow_total() > 1e-8:
        tg("⚠️ borrow≠0 감지 — carry 주문 중단(안전)"); return
    return session.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market",
                               qty=str(round(qty, 3)), reduceOnly=(side == "Buy"))


def run_once():
    init_db()
    f = trailing_funding(); pp = price("linear"); sp = price("spot"); basis = (pp / sp - 1) * 100
    short = perp_short_size(); btc = btc_holdings()
    # 목표 숏 = min(carry할당/현재가, 보유 BTC). 두 번째 항이 naked 숏을 막는 핵심.
    target = min(CARRY_ALLOC / sp, btc)
    target = round(target, 3)
    action = "HOLD"; note = ""

    # 재헤지 — 추세봇이 BTC를 판 직후엔 숏 > 보유 BTC 상태가 됨. 즉시 축소.
    if short > btc + QTY_STEP:
        short_order("Buy", short - btc); action = "REHEDGE"; note = f"숏>보유BTC, {round(short-btc,3)} 축소"
        tg(f"{MTAG} ⚠️ 재헤지: 숏({short})>보유BTC({btc:.4f}) → {round(short-btc,3)} 축소(naked 방지)")
    # 진입/증대 — 펀딩이 충분히 좋고 현재 숏이 목표보다 작으면 차이만큼 추가
    elif f > FUND_THRESHOLD and short < target - QTY_STEP:
        short_order("Sell", target - short); action = "ENTER"
        note = f"펀딩 {f*100:.3f}%/일 → 숏 {round(target-short,3)} 진입(목표 {target})"
        tg(f"{MTAG} 🟢 carry 진입: perp SHORT +{round(target-short,3)} BTC (총 {target}) | 펀딩 {f*100:.3f}%/일, basis {basis:.2f}%. 델타중립.")
    # 청산 — 펀딩이 마이너스로 돌면 carry 수익원이 사라지므로 전량 종료
    elif f <= 0 and short > 0:
        short_order("Buy", short); action = "EXIT"; note = f"펀딩 {f*100:.3f}%/일 ≤0 → 숏 {short} 청산"
        tg(f"{MTAG} 🔴 carry 청산: 숏 {short} BTC 종료 | 펀딩 {f*100:.3f}%/일 ≤0.")

    log_row(f, basis, short, btc, action, note)
    log.info(f"{MTAG} funding={f*100:.3f}%/d basis={basis:.2f}% short={short} btc={btc:.5f} target={target} action={action}")


def main():
    once = "--once" in sys.argv
    tg(f"{MTAG} Carry bot 시작 (1x 무차입, 펀딩>{FUND_THRESHOLD*100:.2f}%/일 진입, 할당 ${CARRY_ALLOC:.0f})")
    while True:
        try: run_once()
        except Exception as e:
            log.exception("err"); tg(f"⚠️ carry 봇 오류: {str(e)[:200]}")
        if once: break
        time.sleep(CHECK)


if __name__ == "__main__":
    main()
