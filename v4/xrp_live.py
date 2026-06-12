#!/usr/bin/env python3
"""
XRP 박스 MR 라이브 봇 — Keltner 하단 매수 / 평균선 익절 / 1.5x perp.

paper_xrp.py 로직을 그대로 실전화한 버전. Bybit REAL 주문을 내고, TP/SL은
서버 측에서 자동으로 트리거되도록 같이 등록한다(봇 다운 시에도 보호됨).

룰:
  - 진입: 일봉 종가 < Keltner_lower AND Keltner 폭이 5~80% 범위 AND BTC 필터 통과
  - 익절: 중심선(midline) 도달
  - 손절: lower - 1.0 × ATR14
  - 시간 손절: 진입 후 20일
  - 청산 후 쿨다운 3일
  - BTC 강제 청산: BTC가 추세 깨거나 변동성 폭증 시

할당: SLEEVE_CAPITAL_USD (현재 $300). notional = sleeve × 1.5.
DB: xrp_live.db. 별도 서비스로 운영.
"""
import os, sqlite3, datetime, math, time, sys
from pybit.unified_trading import HTTP

# === 핵심 파라미터 ===
SLEEVE_CAPITAL_USD = 300.0    # XRP sleeve 할당 (변경 시 이 한 줄만)
LEV = 1.5
SMA_N = 20; ATR_N = 14
KELTNER_K = 1.5
SL_ATR_MULT = 1.0
TIME_STOP_DAYS = 20
COOLDOWN_DAYS = 3
WIDTH_MIN, WIDTH_MAX = 0.05, 0.80   # 박스 너비 필터: 너무 좁으면 노이즈, 너무 넓으면 변동성 폭주
SYMBOL = "XRPUSDT"
INTERVAL = int(os.getenv("XRP_LIVE_INTERVAL", "600"))    # 10분 cycle
DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "xrp_live.db")

s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS xstate(id INTEGER PRIMARY KEY,
        entry_date TEXT, entry_price REAL, qty REAL, tp_price REAL, sl_price REAL,
        last_lev_date TEXT, cooldown_until_date TEXT, days_held INTEGER);
    CREATE TABLE IF NOT EXISTS xlog(ts TEXT, event TEXT, detail TEXT);
    """); c.commit(); c.close()


def log(event, detail=""):
    c = db()
    c.execute("INSERT INTO xlog VALUES(?,?,?)", (datetime.datetime.utcnow().isoformat(), event, str(detail)[:300]))
    c.commit(); c.close()
    print(f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {event}: {str(detail)[:200]}")


def get_xrp_price():
    return float(s.get_tickers(category="linear", symbol=SYMBOL)["result"]["list"][0]["lastPrice"])


def get_xrp_position():
    """Bybit이 가진 실제 포지션을 진실의 원천으로 사용 (DB는 보조 메타)."""
    try:
        for p in s.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]:
            sz = float(p.get("size", 0) or 0)
            if sz > 0:
                return dict(side=p["side"], size=sz, entry=float(p["avgPrice"]),
                            unrealized=float(p.get("unrealisedPnl", 0) or 0))
        return None
    except Exception as e:
        log("pos_check_err", str(e)); return None


def xrp_daily_ohlc():
    """완성된 XRP 일봉만 반환."""
    r = s.get_kline(category="linear", symbol=SYMBOL, interval="D", limit=200)["result"]["list"]
    rows = sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r), key=lambda z: z[0])
    t0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    return [x for x in rows if x[0] < t0]


def compute_keltner(rows):
    """Keltner 채널: 중심 = SMA20, 상/하단 = 중심 ± K × ATR14. 반환: (sma20, atr14, lower, mid, upper)."""
    if len(rows) < max(SMA_N, ATR_N+1): return None
    closes = [r[4] for r in rows[-SMA_N:]]
    sma20 = sum(closes)/SMA_N
    tr = []
    for i in range(len(rows)-ATR_N, len(rows)):
        h, l, prev_c = rows[i][2], rows[i][3], rows[i-1][4]
        tr.append(max(h-l, abs(h-prev_c), abs(l-prev_c)))
    atr14 = sum(tr)/ATR_N
    lower = sma20 - KELTNER_K * atr14
    upper = sma20 + KELTNER_K * atr14
    return sma20, atr14, lower, sma20, upper


def btc_filter_ok():
    """BTC risk-on 필터: BTC가 SMA125 위에 있고 단기 변동성이 폭주하지 않을 때만 진입 허용."""
    try:
        r = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=200)["result"]["list"]
        rows = sorted(([int(x[0]), float(x[4])] for x in r), key=lambda z: z[0])
        t0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
        closes = [x[1] for x in rows if x[0] < t0]
        if len(closes) < 125: return True
        sma125 = sum(closes[-125:])/125
        if closes[-1] <= sma125: return False
        if len(closes) >= 60:
            r20 = [closes[i]/closes[i-1]-1 for i in range(len(closes)-19, len(closes))]
            r60 = [closes[i]/closes[i-1]-1 for i in range(len(closes)-59, len(closes))]
            v20 = (sum((x-sum(r20)/20)**2 for x in r20)/20)**0.5*math.sqrt(365)
            v60 = (sum((x-sum(r60)/60)**2 for x in r60)/60)**0.5*math.sqrt(365)
            if v60 > 0 and v20/v60 >= 1.5: return False
        return True
    except Exception:
        return True


def btc_force_exit():
    """진입 후 BTC 환경이 급격히 악화되면 강제 청산. 진입 필터보다 더 엄격한 조건."""
    try:
        r = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=200)["result"]["list"]
        rows = sorted(([int(x[0]), float(x[4])] for x in r), key=lambda z: z[0])
        t0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
        closes = [x[1] for x in rows if x[0] < t0]
        if len(closes) < 125: return False
        sma125 = sum(closes[-125:])/125
        if closes[-1] < sma125 * 0.98: return True
        if len(closes) >= 60:
            r20 = [closes[i]/closes[i-1]-1 for i in range(len(closes)-19, len(closes))]
            r60 = [closes[i]/closes[i-1]-1 for i in range(len(closes)-59, len(closes))]
            v20 = (sum((x-sum(r20)/20)**2 for x in r20)/20)**0.5*math.sqrt(365)
            v60 = (sum((x-sum(r60)/60)**2 for x in r60)/60)**0.5*math.sqrt(365)
            if v60 > 0 and v20/v60 >= 2.0: return True
        if len(closes) >= 2 and (closes[-1]/closes[-2]-1) <= -0.08: return True
        return False
    except Exception:
        return False


def set_leverage_safe():
    """레버리지 1.5x로 설정. 이미 같으면 Bybit이 'not modified' 에러를 내는데 이건 무시."""
    try:
        s.set_leverage(category="linear", symbol=SYMBOL,
                       buyLeverage=str(LEV), sellLeverage=str(LEV))
        log("set_leverage", f"{LEV}x")
    except Exception as e:
        if "leverage not modified" in str(e).lower(): pass
        else: log("set_leverage_err", str(e))


def place_buy(qty, tp_price, sl_price):
    """시장가 매수와 동시에 서버 측 TP/SL 등록. 봇이 죽어도 보호가 유지됨."""
    try:
        r = s.place_order(category="linear", symbol=SYMBOL, side="Buy",
                          orderType="Market", qty=str(qty), positionIdx=0,
                          takeProfit=str(round(tp_price, 4)), stopLoss=str(round(sl_price, 4)),
                          tpTriggerBy="LastPrice", slTriggerBy="LastPrice",
                          tpslMode="Full")
        log("BUY_PLACED", f"qty={qty} tp={tp_price:.4f} sl={sl_price:.4f} resp={r.get('retMsg')}")
        return True
    except Exception as e:
        log("BUY_ERR", str(e)); return False


def close_position(reason):
    """시장가 전량 청산 (reduceOnly로 다른 포지션 영향 방지)."""
    pos = get_xrp_position()
    if not pos: return
    try:
        r = s.place_order(category="linear", symbol=SYMBOL, side="Sell",
                          orderType="Market", qty=str(pos["size"]), positionIdx=0,
                          reduceOnly=True)
        log("CLOSE_PLACED", f"qty={pos['size']} reason={reason} resp={r.get('retMsg')}")
        return True
    except Exception as e:
        log("CLOSE_ERR", str(e)); return False


def cycle():
    init()
    rows = xrp_daily_ohlc()
    if not rows: log("no_data", ""); return
    k = compute_keltner(rows)
    if not k: log("not_enough_data", ""); return
    sma20, atr14, lower, mid, upper = k
    width_pct = (upper - lower)/mid if mid > 0 else 0
    price = get_xrp_price()
    pos = get_xrp_position()
    btc_ok = btc_filter_ok()
    btc_off = btc_force_exit()
    date = datetime.datetime.utcfromtimestamp(rows[-1][0]/1000).strftime("%Y-%m-%d")

    c = db()
    st = c.execute("SELECT * FROM xstate WHERE id=1").fetchone()
    if st is None:
        c.execute("INSERT INTO xstate VALUES(1,'',0,0,0,0,?,'',0)", (date,))
        c.commit()
        st = c.execute("SELECT * FROM xstate WHERE id=1").fetchone()
    last_lev_date = st["last_lev_date"]
    cooldown = st["cooldown_until_date"] or ""
    days_held = st["days_held"] or 0
    entry_date = st["entry_date"] or ""
    entry_price = st["entry_price"] or 0

    # ── 포지션 보유 중 ──
    if pos:
        # 날짜가 바뀌면 days_held 증가 (하루 1번만)
        if date != last_lev_date:
            days_held += 1
            c.execute("UPDATE xstate SET days_held=?, last_lev_date=? WHERE id=1", (days_held, date))
            c.commit()
        # BTC 강제 청산 — 환경 악화 시 즉시 종료
        if btc_off:
            log("BTC_OFF_TRIGGER", "BTC risk-off → XRP 강제 청산")
            close_position("BTC_OFF")
            cooldown = (datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
            c.execute("UPDATE xstate SET entry_date='', entry_price=0, qty=0, tp_price=0, sl_price=0, days_held=0, cooldown_until_date=? WHERE id=1", (cooldown,))
        # 시간 손절
        elif days_held >= TIME_STOP_DAYS:
            log("TIME_STOP_TRIGGER", f"{days_held}일 보유, 강제 청산")
            close_position("TIME_STOP")
            cooldown = (datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
            c.execute("UPDATE xstate SET entry_date='', entry_price=0, qty=0, tp_price=0, sl_price=0, days_held=0, cooldown_until_date=? WHERE id=1", (cooldown,))
        # 그 외엔 Bybit이 서버 TP/SL을 자동 처리 — 우리는 모니터만
    else:
        # ── 포지션 없음 — 매 cycle마다 진입 가능 여부 확인 ──
        # 직전 사이클까지 entry_date가 있었는데 지금 포지션이 사라졌다 = 외부 청산(TP/SL/수동)
        if entry_date:
            log("POSITION_CLOSED_EXT", f"이전 진입 {entry_date} → 포지션 사라짐 (Bybit TP/SL)")
            cooldown = (datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
            c.execute("UPDATE xstate SET entry_date='', entry_price=0, qty=0, tp_price=0, sl_price=0, days_held=0, cooldown_until_date=? WHERE id=1", (cooldown,))
            entry_date = ""
        # 진입 조건 확인 — 하루에 한 번만 WAIT 로그 (cycle 10분마다 spam 방지)
        in_cooldown = bool(cooldown) and date <= cooldown
        width_ok = WIDTH_MIN <= width_pct <= WIDTH_MAX
        if in_cooldown:
            if date != last_lev_date:
                log("WAIT", f"cooldown until {cooldown}")
                c.execute("UPDATE xstate SET last_lev_date=? WHERE id=1", (date,))
        elif not width_ok:
            if date != last_lev_date:
                log("WAIT", f"width {width_pct*100:.1f}% out of range")
                c.execute("UPDATE xstate SET last_lev_date=? WHERE id=1", (date,))
        elif not btc_ok:
            if date != last_lev_date:
                log("WAIT", "BTC risk-off")
                c.execute("UPDATE xstate SET last_lev_date=? WHERE id=1", (date,))
        elif price > lower:
            if date != last_lev_date:
                log("WAIT", f"price ${price:.4f} > lower ${lower:.4f} ({((price/lower-1)*100):+.2f}%)")
                c.execute("UPDATE xstate SET last_lev_date=? WHERE id=1", (date,))
        else:
            # 모든 조건 만족 → 진입
            set_leverage_safe()
            notional = SLEEVE_CAPITAL_USD * LEV
            qty = round(notional / price, 0)
            if qty < 4:                                              # Bybit XRP perp 최소 ~$5 notional
                log("QTY_TOO_SMALL", f"notional ${notional:.0f} / price ${price:.4f} = {qty}")
            else:
                tp_px = mid
                sl_px = lower - SL_ATR_MULT * atr14
                log("ENTRY_SIGNAL", f"price=${price:.4f} lower=${lower:.4f} mid=${mid:.4f} sl=${sl_px:.4f} qty={qty} notional=${qty*price:.2f}")
                if place_buy(qty, tp_px, sl_px):
                    c.execute("UPDATE xstate SET entry_date=?, entry_price=?, qty=?, tp_price=?, sl_price=?, days_held=0, last_lev_date=?, cooldown_until_date='' WHERE id=1",
                              (date, price, qty, tp_px, sl_px, date))
    c.commit(); c.close()


def main():
    once = "--once" in sys.argv
    init()
    log("STARTUP", f"sleeve=${SLEEVE_CAPITAL_USD} lev={LEV}x interval={INTERVAL}s symbol={SYMBOL}")
    while True:
        try: cycle()
        except Exception as e: log("cycle_err", str(e))
        if once: break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
