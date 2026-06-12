#!/usr/bin/env python3
"""전체 시스템 진단 덤프 — 2-sleeve(추세+carry) + 펀딩 + 페이퍼 변형 + AI 결정 로그."""
import sqlite3, os, datetime, math
from pybit.unified_trading import HTTP

s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))
D = "/home/botuser/metis-v4"


def q1(db, sql):
    try:
        c = sqlite3.connect(f"{D}/{db}"); r = c.execute(sql).fetchall(); c.close(); return r
    except Exception as e:
        return [("err", str(e)[:60])]


# 시세 + 24h 변동률
t = s.get_tickers(category="spot", symbol="BTCUSDT")["result"]["list"][0]
px = float(t["lastPrice"]); chg = float(t.get("price24hPcnt", 0)) * 100
# SMA125 — Bybit 현물 일봉의 *완성봉*만 사용 (오늘 진행 봉 제외)
k = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=200)["result"]["list"]
rows = sorted(([int(x[0]), float(x[4])] for x in k), key=lambda z: z[0])
today0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
closes = [r[1] for r in rows if r[0] < today0]
sma = sum(closes[-125:]) / 125; lastclose = closes[-1]
# 잔고 + perp 숏 (carry sleeve) + 차입(있으면 안전 위반 경보)
coins = {c["coin"]: float(c.get("walletBalance", 0) or 0) for c in s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]}
usdt = coins.get("USDT", 0); btc = coins.get("BTC", 0)
borrow = sum(float(c.get("borrowAmount", 0) or 0) for c in s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"])
short = 0.0
for p in s.get_positions(category="linear", symbol="BTCUSDT")["result"]["list"]:
    if p.get("side") == "Sell": short = float(p.get("size", 0) or 0)
equity = usdt + btc * px + coins.get("USDC", 0)
# 펀딩 (직전 3일 평균, 일평균%로 환산)
fr = [float(x["fundingRate"]) for x in s.get_funding_rate_history(category="linear", symbol="BTCUSDT", limit=12)["result"]["list"]]
fund_day = sum(fr[:9]) / 9 * 3 if fr else 0
# DB에서 원금/시작시각/추세상태/carry 이력 조회
meta = dict(q1("metis_v4.db", "select k,val from meta"))
baseline = float(meta.get("initial_equity", 1002)); start = meta.get("start_ts")
state = q1("metis_v4.db", "select position,entry_price from state where id=1")
carrylog = q1("metis_v5_carry.db", "select ts,round(funding*100,3),short_btc,action from carry_log order by rowid desc limit 4")
days = ((datetime.datetime.utcnow() - datetime.datetime.fromisoformat(start)).total_seconds() / 86400) if start else 1
# 페이퍼 변형 — 과거 운영했던 모든 이름 포함(과거 데이터가 DB에 남아있을 수 있음)
preal = (equity - baseline) / baseline * 100
PVORDER = ("P0_1x", "P1_1.5x", "S0_2x_shadow", "S1_buyhold",
           "V_vt2", "V_vt3", "V_vt5", "V_vt10", "V_mom", "V_rb",
           "V_ai_pro", "V_ai_flash", "V_ai_full",
           "V_ai_pro_free", "V_ai_flash_free",
           "V_ai_pro_aggro", "V_ai_flash_aggro",
           "V_ai_pro_aggro_safe", "V_ai_flash_aggro_safe", "V_tp10")
pvar = {}
for v in PVORDER:
    rws = q1("paper_lev.db", f"select equity,leverage from pequity where variant='{v}' order by ts")
    if rws and rws[0][0] != "err" and len(rws):
        eqs = [float(r[0]) for r in rws]; pk = eqs[0]; mdd = 0
        for x in eqs:
            pk = max(pk, x); mdd = min(mdd, (x-pk)/pk*100)
        pvar[v] = (len(eqs), (eqs[-1]/eqs[0]-1)*100, mdd, float(rws[-1][1]))

print(f"자산: ${equity:.2f} | 원금 ${baseline:.0f} | 수익 ${equity-baseline:+.2f} ({(equity-baseline)/baseline*100:+.2f}%) | 운영 {days:.1f}일 | 빚 {borrow}")
print(f"보유: BTC {btc:.6f} (${btc*px:.0f}) + USDT ${usdt:.0f}")
print(f"추세: {state} | perp숏(carry): {short}")
print(f"시장: BTC ${px:,.0f} (24h {chg:+.1f}%) | SMA125 ${sma:,.0f} | 마지막종가 ${lastclose:,.0f} ({(lastclose/sma-1)*100:+.1f}%) | 청산선 ${sma*0.98:,.0f}({(sma*0.98/px-1)*100:+.1f}%)")
print(f"펀딩: 직전3일평균 {fund_day*100:.3f}%/일 (문턱 0.06%, 현재 {'진입조건충족' if fund_day>0.0006 else '미달→HOLD'})")
print("carry 로그(최근):")
for r in carrylog: print("  ", r)
# ── 페이퍼 레버 검증 요약 ──
print("\n[페이퍼 레버 검증 — P0=1x기준 / P1=1.5x실전후보 / S0=2x shadow(실전X) / S1=buy&hold]")
print(f"  실제 라이브 시스템 수익: {preal:+.2f}%")
if pvar:
    print(f"  {'변형':<16}{'일수':>4}{'수익%':>8}{'MDD%':>7}{'현재레버':>8}{'vsP0':>8}")
    p0 = pvar.get("P0_1x", (0, 0, 0, 0))[1]
    for v in PVORDER:
        if v not in pvar: continue
        n, ret, mdd, lev = pvar[v]
        print(f"  {v:<16}{n:>4}{ret:>+8.2f}{mdd:>7.1f}{lev:>7.2f}x{ret-p0:>+7.2f}p")
    print(f"  참고(백테스트): 레버 단순 상승은 엣지 X (Sharpe 비슷). 1.5x는 수익↑이지만 MDD -62%, 2x는 -76%.")
    print(f"  운영검증 목적: 신호/펀딩/수수료/청산가/리밸런스 오차 0 + 수동개입 0이면 operational GO.")
    print(f"  표본 {pvar['P0_1x'][0]}포인트(10분 단위). 단기 수익률은 노이즈이므로 판단 근거 아님.")
else:
    print("  데이터 수집 시작 단계 (아직 비교 불가)")

# ── grade + AI 결정 로그 ──
bm = q1("paper_lev.db", "select prev_trend_grade,prev_risk_grade,prev_funding_grade,prev_extension_grade,chop_persist_days,favorable_persist_days from base_meta where id=1")
if bm and bm[0][0] != "err":
    tg, rg, fg, eg, cp, fp = bm[0]
    print(f"\n[Grade] trend={tg} risk={rg} funding={fg} extension={eg} chop_persist={cp} fav_persist={fp}")
ailog = q1("paper_lev.db", "select variant,date,raw_leverage,effective_leverage,violation,ok,reason from ai_log order by rowid desc limit 9")
if ailog and ailog[0][0] != "err":
    print("\n[AI 레버 결정 (최근, 감사로그)]")
    for v, d, rl, eff, viol, ok, reason in ailog:
        tag = "VIOL" if viol else ("OK" if ok else "FAIL")
        diff = f" raw={rl}" if rl != eff else ""
        print(f"  {v:<12} {d} L={eff}{diff} {tag} | {str(reason)[:75]}")
