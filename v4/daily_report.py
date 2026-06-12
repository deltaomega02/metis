#!/usr/bin/env python3
"""
일일 자산 리포트 — 텔레그램 발송.

숫자는 모두 코드가 정확히 집계(원금/현재자산/수익률/일평균/추세상태/펀딩 등)하고,
Gemini Flash는 *문장만* 작성한다. 모델이 새 숫자를 지어내지 못하도록 프롬프트에
"위 숫자만 사용, 새 숫자 생성 금지" 를 명시. systemd timer로 매일 1회 실행.

env: BYBIT_API_KEY/SECRET, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN/CHAT_ID
"""
import os, sqlite3, datetime, math
import requests
from pybit.unified_trading import HTTP

DIR = os.path.dirname(os.path.abspath(__file__))
TREND_DB = os.path.join(DIR, "metis_v4.db")
CARRY_DB = os.path.join(DIR, "metis_v5_carry.db")
API_KEY = os.getenv("BYBIT_API_KEY"); API_SEC = os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN"); TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
s = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SEC)


def tg(m):
    if TG_TOKEN and TG_CHAT:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": m}, timeout=15)


def gather():
    """리포트에 필요한 모든 숫자를 한 번에 수집."""
    # 시세 + 24시간 변동률
    t = s.get_tickers(category="spot", symbol="BTCUSDT")["result"]["list"][0]
    px = float(t["lastPrice"]); chg = float(t.get("price24hPcnt", 0)) * 100
    # 지갑 잔고
    coins = {c["coin"]: float(c.get("walletBalance", 0) or 0) for c in s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]}
    usdt = coins.get("USDT", 0); btc = coins.get("BTC", 0)
    # carry용 perp 숏 사이즈
    short = 0.0
    for p in s.get_positions(category="linear", symbol="BTCUSDT")["result"]["list"]:
        if p.get("side") == "Sell": short = float(p.get("size", 0) or 0)
    equity = usdt + btc * px + coins.get("USDC", 0)
    # 원금/시작시각은 추세봇 DB의 meta에 저장돼 있음
    baseline = 1002.0; start = None
    try:
        c = sqlite3.connect(TREND_DB); m = dict(c.execute("select k,val from meta").fetchall()); c.close()
        baseline = float(m.get("initial_equity", 1002)); start = m.get("start_ts")
    except Exception: pass
    days = 1.0
    if start:
        try: days = max((datetime.datetime.utcnow() - datetime.datetime.fromisoformat(start)).total_seconds() / 86400, 0.04)
        except Exception: pass
    # 추세 sleeve 현재 상태
    tpos = "현금"; tentry = None
    try:
        c = sqlite3.connect(TREND_DB); r = c.execute("select position,entry_price from state where id=1").fetchone(); c.close()
        if r: tpos = r[0]; tentry = r[1]
    except Exception: pass
    # carry sleeve — 마지막 펀딩 신호값
    funding = None
    try:
        c = sqlite3.connect(CARRY_DB); r = c.execute("select funding from carry_log order by rowid desc limit 1").fetchone(); c.close()
        if r: funding = r[0]
    except Exception: pass
    profit = equity - baseline; pct = profit / baseline * 100 if baseline else 0
    # 일평균 수익률(기하): (equity/baseline)^(1/days) - 1. 운영 초기엔 노이즈 크므로 참고용.
    daily = (equity / baseline) ** (1 / days) - 1 if baseline > 0 and equity > 0 else 0
    return dict(px=px, chg=chg, usdt=usdt, btc=btc, short=short, equity=equity, baseline=baseline,
                days=days, profit=profit, pct=pct, daily=daily, tpos=tpos, tentry=tentry, funding=funding)


def make_report(d):
    """수집한 숫자로 리포트 문장을 Gemini Flash에 위임 생성."""
    proj_m = d["baseline"] * ((1 + d["daily"]) ** 30 - 1)
    proj_y = d["baseline"] * ((1 + d["daily"]) ** 365 - 1)
    upnl = ((d["px"] / d["tentry"] - 1) * 100) if d["tentry"] else 0
    carry_txt = "대기중(펀딩 낮음)" if d["short"] == 0 else f"가동중(헤지숏 {d['short']} BTC)"
    fund_txt = f"{d['funding']*100:.3f}%/일" if d["funding"] is not None else "?"
    # 페이퍼 레버 검증 현황은 같은 폴더의 paper_leverage 모듈에서 가져옴(없으면 패스)
    paper_line = "페이퍼 레버 검증: 데이터 수집 시작 전"
    try:
        import paper_leverage
        ps = paper_leverage.status()
        if ps:
            parts = ", ".join(f"{k} {v['ret']:+.1f}%(MDD{v['mdd']:.0f})" for k, v in ps.items())
            paper_line = f"페이퍼 레버 검증(실제 아님, P0 1x기준/P1 1.5x후보/S0 2x shadow실전X/S1 buy&hold) — {parts} | 실제 시스템 {d['pct']:+.2f}%"
    except Exception:
        pass
    data = f"""[정확한 숫자 — 이것만 사용, 새 숫자 지어내기 금지]
원금: ${d['baseline']:.2f}
현재 총자산: ${d['equity']:.2f}
총 수익: ${d['profit']:+.2f} ({d['pct']:+.2f}%)
운영 기간: {d['days']:.1f}일
일평균 수익률: {d['daily']*100:+.2f}%/일
현재 페이스 단순환산 — 월: ${proj_m:+.0f}, 연: ${proj_y:+.0f}
BTC 가격: ${d['px']:,.0f} (24시간 {d['chg']:+.1f}%)
추세 sleeve: {d['tpos']}{f" (진입 ${d['tentry']:,.0f}, 미실현 {upnl:+.1f}%)" if d['tentry'] else ""}
carry sleeve: {carry_txt}, 펀딩 {fund_txt}
빚: 0 (무차입)
{paper_line}"""
    instr = """위 숫자만 써서, 코인 문외한인 사용자에게 보내는 *오늘의 자산 리포트*를 한국어로 작성해.
- 1) 원금 대비 지금 자산/수익이 어떤지 2) 시스템이 지금 뭘 하고 있는지(추세 보유/현금, carry 대기/가동) 3) "이 기세면 대략" 환산.
- ⚠️ 운영 7일 미만이면 "아직 초기라 페이스 환산은 변동이 커서 참고만" 꼭 명시. 위로성 과장·낙관 금지, 정직하게.
- '페이퍼 레버' 줄이 있으면 마지막에 한 문장으로 "참고: 레버 전략을 가상으로 검증 중(실제 돈 아님), 현재 페이퍼 수익률이 실제 시스템보다 좋은지/나쁜지" 덧붙여. 좋다고 부추기지 말고 사실만.
- 쉬운 말, 4~6문장(+페이퍼 참고 1문장), 이모지 약간. 숫자는 위 값 그대로(지어내기 절대 금지)."""
    try:
        from google import genai
        from google.genai import types
        cli = genai.Client(api_key=GEMINI_KEY)
        r = cli.models.generate_content(
            model="gemini-3.5-flash",
            contents=data + "\n\n" + instr,
            config=types.GenerateContentConfig(
                temperature=0.4, max_output_tokens=3000,   # thinking LOW가 ~768 토큰 차지하므로 넉넉히
                thinking_config=types.ThinkingConfig(thinking_level="LOW")))
        return "📊 일일 자산 리포트\n\n" + (r.text or "(생성 실패)")
    except Exception as e:
        # 모델 호출 실패 시 폴백: 숫자만 보내고 사유를 함께 표시
        return f"📊 일일 자산 리포트 (수동)\n원금 ${d['baseline']:.0f} → 현재 ${d['equity']:.2f} ({d['pct']:+.2f}%)\n추세:{d['tpos']} carry:{carry_txt}\n(Flash 오류: {str(e)[:80]})"


if __name__ == "__main__":
    d = gather()
    tg(make_report(d))
    print("리포트 발송 완료")
