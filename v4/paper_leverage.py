#!/usr/bin/env python3
"""
페이퍼 레버리지 검증 — 실시간 forward-test (실주문 0, 시뮬만).

같은 BTC 가격 스트림 위에서 여러 레버리지 전략을 동시에 시뮬레이션하여 어느 전략이
실전 후보로 적합한지 가린다. 각 변형(variant)은 자기만의 가상 자본/포지션/펀딩/수수료를
계산하며 결과는 paper_lev.db에 저장.

변형 그룹:
  - 고정 레버: P0_1x (기준선), P1_1.5x (실전 후보), S1_buyhold (벤치마크)
  - Vol-target: V_vt2 / V_vt5 / V_vt10 (변동성 역수 사이저, 천장만 다름)
  - AI 사이저(코드가 grade 강제): V_ai_pro, V_ai_flash, V_ai_full
  - AI 자율(룰 없이 원칙만): V_ai_pro_free, V_ai_flash_free
  - AI 공격(고레버 적극): V_ai_pro_aggro, V_ai_flash_aggro
  - AI 공격+게이트(약세장 강제 현금): V_ai_pro_aggro_safe, V_ai_flash_aggro_safe
  - 단타 데모: V_tp10 (10x + ATR 적응형 TP + 1일 쿨다운)

감사: AI 호출은 raw_leverage(모델 출력)와 effective_leverage(코드 검증 후 실제 적용값)를
모두 ai_log에 남긴다. 둘이 다르면 violation=1로 표시.
"""
import os, sqlite3, datetime, math, time, sys, json, re
from pybit.unified_trading import HTTP

SMA_N = 125; EXIT_BUF = 0.98; MM = 0.005; LB = 20
INTERVAL = int(os.getenv("METIS_PAPER_INTERVAL", "600"))
DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "paper_lev.db")
FEE_PERP = 0.0006; FEE_SPOT = 0.001
VARIANTS = ("P0_1x", "P1_1.5x", "S1_buyhold",
            "V_vt2", "V_vt5", "V_vt10",
            "V_ai_pro", "V_ai_flash", "V_ai_full",
            "V_ai_pro_free", "V_ai_flash_free",
            "V_ai_pro_aggro", "V_ai_flash_aggro",
            "V_ai_pro_aggro_safe", "V_ai_flash_aggro_safe",
            "V_tp10")
# 정리 이력: S0_2x_shadow(V_vt2와 중복) · V_vt3(vt2/5 중간) · V_mom(엣지 약함) · V_rb(백테스트 실패) → 제거
TP10_LEV = 10.0
# V_tp10 적응형 TP: 레버 × ATR20% × 0.5, [4%, 15%]로 클램프. 변동성이 클수록 TP를 넓혀 노이즈 회피.
def tp10_target(lev, atr20_pct):
    return clip(lev * (atr20_pct/100.0) * 0.5, 0.04, 0.15)
FIXED = {"P0_1x": 1.0, "P1_1.5x": 1.5, "S0_2x_shadow": 2.0, "S1_buyhold": 1.0}
AI_MODELS = {"V_ai_pro": "gemini-3.1-pro-preview", "V_ai_flash": "gemini-3.5-flash",
             "V_ai_full": "gemini-3.1-pro-preview",
             "V_ai_pro_free": "gemini-3.1-pro-preview",
             "V_ai_flash_free": "gemini-3.5-flash",
             "V_ai_pro_aggro": "gemini-3.1-pro-preview",
             "V_ai_flash_aggro": "gemini-3.5-flash",
             "V_ai_pro_aggro_safe": "gemini-3.1-pro-preview",
             "V_ai_flash_aggro_safe": "gemini-3.5-flash"}
AI_FULL = {"V_ai_full"}                                            # 익절 재량까지 AI에 위임
AI_FREE = {"V_ai_pro_free", "V_ai_flash_free"}                     # grade/band/cap 없이 자율(보수 tilt 가이드)
AI_AGGRO = {"V_ai_pro_aggro", "V_ai_flash_aggro"}                  # 자율 + 공격 가이드(천장 10x까지 적극)
AI_AGGRO_SAFE = {"V_ai_pro_aggro_safe", "V_ai_flash_aggro_safe"}   # 공격 + 시스템 게이트: 강확립 추세에서만 AI 호출, 그외 강제 현금
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
ALLOWED = [0, 0.5, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 5.0, 10.0]   # 사용 가능한 레버리지 단계
SPOT = {"P0_1x", "S1_buyhold"}                                       # 현물 시뮬(펀딩 없음, 수수료 spot)
ALWAYS = {"S1_buyhold"}                                              # 추세 무관 항상 1x (buy-and-hold 벤치마크)
s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS pstate(variant TEXT PRIMARY KEY, equity REAL, leverage REAL,
        last_price REAL, active INT, last_lev_date TEXT, last_fund_ts INTEGER, pos_entry_price REAL,
        pos_entry_equity REAL, peak_equity_since_entry REAL, days_at_current_leverage INTEGER,
        cooldown_until_date TEXT);
    CREATE TABLE IF NOT EXISTS pequity(ts TEXT, variant TEXT, price REAL, leverage REAL,
        equity REAL, liq_price REAL, liq_dist REAL, event TEXT);
    CREATE TABLE IF NOT EXISTS daily_snapshot(date TEXT PRIMARY KEY, btc_close REAL, sma125 REAL,
        exit_line REAL, in_trend INTEGER, realized_vol REAL, funding_3d REAL, levs_json TEXT);
    CREATE TABLE IF NOT EXISTS base_meta(id INTEGER PRIMARY KEY, base_state TEXT, entry_price REAL, entry_date TEXT,
        prev_trend_grade TEXT, prev_risk_grade TEXT, prev_funding_grade TEXT, prev_extension_grade TEXT,
        chop_persist_days INTEGER, favorable_persist_days INTEGER, prev_price_vs_sma125 REAL, persist_date TEXT);
    CREATE TABLE IF NOT EXISTS ai_log(ts TEXT, date TEXT, variant TEXT, model TEXT,
        raw_leverage REAL, effective_leverage REAL, violation INTEGER, reason TEXT, raw TEXT, ok INTEGER);
    """)
    # in-place migration — 기존 DB는 보존하면서 컬럼만 추가 (이미 있으면 OperationalError 무시)
    for stmt in ("ALTER TABLE base_meta ADD COLUMN persist_date TEXT",
                 "ALTER TABLE pstate ADD COLUMN cooldown_until_date TEXT"):
        try: c.execute(stmt)
        except sqlite3.OperationalError: pass
    c.commit(); c.close()


def next_day_str(d):
    return (datetime.datetime.strptime(d, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def daily_ohlc():
    """완성된 BTC 일봉 OHLC를 컬럼별 리스트로 반환."""
    r = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=400)["result"]["list"]
    rows = sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r), key=lambda z: z[0])
    t0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    rows = [x for x in rows if x[0] < t0]
    o = [x[1] for x in rows]; h = [x[2] for x in rows]; l = [x[3] for x in rows]; c = [x[4] for x in rows]
    date = datetime.datetime.utcfromtimestamp(rows[-1][0]/1000).strftime("%Y-%m-%d")
    return o, h, l, c, date


def intraday_low_since(since_ms):
    """지난 cycle 이후 1분봉 저가. 장중 wick 청산을 정확히 잡기 위함."""
    r = s.get_kline(category="spot", symbol="BTCUSDT", interval="1", limit=30)["result"]["list"]
    lows = [float(x[3]) for x in r if int(x[0]) >= since_ms - 60000]
    return min(lows) if lows else None


def cur_price():
    return float(s.get_tickers(category="spot", symbol="BTCUSDT")["result"]["list"][0]["lastPrice"])


def latest_funding():
    r = s.get_funding_rate_history(category="linear", symbol="BTCUSDT", limit=5)["result"]["list"]
    return (int(r[0]["fundingRateTimestamp"]), float(r[0]["fundingRate"])) if r else (0, 0.0)


def funding_series(n=50):
    try:
        return [float(x["fundingRate"]) for x in s.get_funding_rate_history(category="linear", symbol="BTCUSDT", limit=n)["result"]["list"]]
    except Exception:
        return []


def clip(x, lo, hi): return max(lo, min(hi, x))
def snap(lev): return min(ALLOWED, key=lambda a: abs(a - lev))   # 가장 가까운 ALLOWED 값으로 스냅


# ───────── 시장 피처 계산 ─────────
def compute_features(o, h, l, c, price, sma125, exit_line, fser):
    """추세/변동성/펀딩/위치/최근 경로 등 의사결정에 쓰이는 모든 피처를 한 번에 계산."""
    def sma(n): return sum(c[-n:])/n
    sma20 = sma(20); sma200 = sma(200) if len(c) >= 200 else sma(len(c))
    sma20_prev5 = sum(c[-25:-5])/20 if len(c) >= 25 else sma20
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(len(c)-20, len(c))]
    atr20 = sum(tr)/len(tr)
    def vol(n):
        r = [c[i]/c[i-1]-1 for i in range(len(c)-n, len(c))]
        m = sum(r)/len(r); return (sum((x-m)**2 for x in r)/len(r))**0.5*math.sqrt(365)
    v20, v60 = vol(20), vol(60)
    # 장중 최악 하락 (양의 크기로 표시) — 청산 안전성 판단에 사용
    wid30 = abs(min((l[i]/c[i-1]-1)*100 for i in range(len(c)-30, len(c))))
    wid90 = abs(min((l[i]/c[i-1]-1)*100 for i in range(len(c)-90, len(c))))
    wcl30 = abs(min((c[i]/c[i-1]-1)*100 for i in range(len(c)-30, len(c))))
    wcl90 = abs(min((c[i]/c[i-1]-1)*100 for i in range(len(c)-90, len(c))))
    hi30 = max(h[-30:]); hi60 = max(h[-60:])
    # 펀딩 통계: 3일평균(일평균%) / 7일/14일 APR (8h × 3 × 365)
    f_day3 = sum(fser[:9])/9*3 if len(fser) >= 9 else 0.0
    f7apr = (sum(fser[:21])/21)*3*365*100 if len(fser) >= 21 else 0.0
    f14apr = (sum(fser[:42])/42)*3*365*100 if len(fser) >= 42 else 0.0
    # stress wick: 청산 안전성 판정용 보수적인 wick 크기 추정
    stress = max(20.0, 3*atr20/price*100, 1.5*wid90)
    # 오늘(완성봉) 캔들 특성
    today_ret = (c[-1]/c[-2]-1)*100 if len(c) >= 2 else 0
    today_range = (h[-1]-l[-1])/c[-2]*100 if len(c) >= 2 else 0
    cir = ((c[-1]-l[-1])/(h[-1]-l[-1])*100) if (h[-1]-l[-1]) > 0 else 50.0     # close-in-range
    # chop 판정 (최근 5일 방향성 부재)
    r5 = [(c[i]/c[i-1]-1)*100 for i in range(len(c)-5, len(c))]
    net5 = abs(sum(r5)); sumabs5 = sum(abs(x) for x in r5)
    eff5 = (net5/sumabs5) if sumabs5 > 0 else 1.0                              # efficiency ratio
    signs = sum(1 for i in range(1, len(r5)) if (r5[i] > 0) != (r5[i-1] > 0))
    chop_flag = (signs >= 3) and (eff5 <= 0.30) and (sumabs5 >= 6)
    last5 = [round(x, 1) for x in r5]
    # 마지막 30일 고가까지 며칠 지났는지 (모멘텀 신선도 지표)
    hi30_idx = max(range(len(c)-30, len(c)), key=lambda i: h[i])
    days_since_30d_high = (len(c)-1) - hi30_idx
    return dict(
        sma20=sma20, sma200=sma200, atr20=atr20, atr20_pct=atr20/price*100,
        v20=v20*100, v60=v60*100, vratio=(v20/v60) if v60 > 0 else 1.0,
        wid30=wid30, wid90=wid90, wcl30=wcl30, wcl90=wcl90,
        stress=stress, hi30=hi30, hi60=hi60,
        price_vs_sma20=(price/sma20-1)*100,
        price_vs_sma125=(price/sma125-1)*100,
        price_vs_exit=(price/exit_line-1)*100,
        price_vs_sma200=(price/sma200-1)*100,
        dist_30dhigh=(price/hi30-1)*100, dist_60dhigh=(price/hi60-1)*100,
        dd_60dhigh=abs((price/hi60-1)*100) if price < hi60 else 0,
        sma20_slope_5d=(sma20/sma20_prev5-1)*100 if sma20_prev5 > 0 else 0,
        days_since_30d_high=days_since_30d_high,
        f_day3=f_day3*100, f7apr=f7apr, f14apr=f14apr,
        cur8h=(fser[0]*100 if fser else 0.0),
        today_ret=today_ret, today_range=today_range, cir=cir,
        net5=net5, sumabs5=sumabs5, eff5=eff5, signs5=signs,
        chop_flag=chop_flag, last5=last5)


# ───────── grade 계산 (코드가 결정, AI에 전달) ─────────
def risk_grade(mf):
    """변동성/wick/ATR 종합 위험 등급. low/medium/high/severe."""
    if any([mf['v20'] >= 110, mf['vratio'] >= 1.75, mf['atr20_pct'] >= 10,
            mf['wid30'] >= 22, mf['wid90'] >= 32, mf['stress'] >= 38]): return 'severe'
    if any([mf['v20'] >= 80, mf['vratio'] >= 1.50, mf['atr20_pct'] >= 7,
            mf['wid30'] >= 18, mf['wid90'] >= 25, mf['stress'] >= 30]): return 'high'
    if any([mf['v20'] >= 60, mf['vratio'] >= 1.20, mf['atr20_pct'] >= 5,
            mf['wid30'] >= 12, mf['wid90'] >= 18, mf['stress'] >= 25]): return 'medium'
    return 'low'


def funding_grade(f7apr):
    """펀딩 7일 APR 기준 carry 비용 등급."""
    if f7apr >= 60: return 'punitive'
    if f7apr >= 25: return 'expensive'
    if f7apr >= 10: return 'normal'
    return 'cheap'


def extension_grade(mf, ret_in):
    """추세 신선도 — 너무 멀리 갔으면 mean-reversion 위험."""
    if any([mf['price_vs_sma125'] >= 30, mf['price_vs_sma20'] >= 15, ret_in >= 60]): return 'extreme'
    if any([mf['price_vs_sma125'] >= 15, mf['price_vs_sma20'] >= 8, ret_in >= 30]): return 'extended'
    return 'normal'


def trend_grade(base_state, base_exit, btc, sma125, mf, days_in, ret_in, ext_g):
    """추세 강도/숙성도 등급. failed/weak_positive/early_positive/confirmed_positive/strong_positive/extended."""
    if base_state == 'CASH' or base_exit: return 'failed'
    if base_state != 'LONG': return 'failed'
    if btc < sma125 or mf['price_vs_sma125'] < 3 or mf['sma20_slope_5d'] <= 0: return 'weak_positive'
    if ext_g == 'extreme': return 'extended'
    if days_in <= 7 or ret_in < 8 or mf['price_vs_sma200'] < 0: return 'early_positive'
    if days_in >= 14 and ret_in >= 15 and mf['sma20_slope_5d'] > 0 and mf['price_vs_sma200'] > 0:
        return 'strong_positive'
    if days_in > 7 and mf['sma20_slope_5d'] > 0: return 'confirmed_positive'
    return 'early_positive'


def impulse_flag(mf, prev_pvs125):
    """오늘 캔들이 비정상적으로 큰지 — 즉시 추가 레버 금지용."""
    chg = (mf['price_vs_sma125'] - prev_pvs125) if prev_pvs125 is not None else 0
    return (mf['today_ret'] >= 6) or (mf['today_range'] >= 2.0*mf['atr20_pct']) or (chg >= 7)


def target_band(tg):
    """추세 등급에 따른 권장 레버 범위."""
    return {'failed': (0, 0), 'weak_positive': (0.5, 1.0), 'early_positive': (1.0, 1.25),
            'confirmed_positive': (1.25, 1.5), 'strong_positive': (1.5, 2.0),
            'extended': (1.0, 1.25)}.get(tg, (1.0, 1.25))


def hard_cap(rg, fg, eg, chop_persist):
    """위험/펀딩/extension/chop 등급들이 부과하는 하드 캡 중 최솟값."""
    risk_cap = {'low': 2.0, 'medium': 1.5, 'high': 1.25, 'severe': 1.0}[rg]
    fund_cap = {'cheap': 2.0, 'normal': 2.0, 'expensive': 1.5, 'punitive': 1.25}[fg]
    ext_cap = {'normal': 2.0, 'extended': 1.5, 'extreme': 1.25}[eg]
    chop_cap = 1.25 if chop_persist >= 3 else 10.0
    return min(risk_cap, fund_cap, ext_cap, chop_cap)


def liq_safety_ok(L, stress):
    """청산까지 거리(drop=99.5/L)가 stress wick 대비 충분한지 — 레버 별 안전 비율 요구."""
    if L <= 0: return True
    drop = 99.5/L; ratio = drop/stress if stress > 0 else 0
    if L <= 1.0: return ratio >= 1.50
    if L <= 1.5: return ratio >= 2.00
    if L <= 2.0: return ratio >= 2.25
    return ratio >= 3.00


def allowed_set(band, cap, stress, base_state):
    """이번 cycle에서 AI에 제시할 합법 레버 후보 셋. 0은 항상 포함."""
    if base_state == 'CASH': return [0]
    lo, hi = band; hi = min(hi, cap)
    return [L for L in ALLOWED if (L == 0) or (lo <= L <= hi and liq_safety_ok(L, stress))]


def profit_flags(eq_ret, eq_peak_ret, eq_giveback, f7apr, vratio, rg, cir, today_range, atr20_pct,
                 ext_g, pvs20, sma20_slope, wid30, stress, today_ret):
    """V_ai_full의 익절 신호 8개. 둘 이상 켜지면 단계 감축 검토."""
    P1 = eq_peak_ret >= 30                                                                       # 큰 수익 누적
    P2 = (eq_giveback >= 10) or (eq_peak_ret >= 30 and eq_giveback >= 7 and today_ret <= -6 and cir <= 25)  # giveback
    P3 = f7apr >= 40                                                                              # 펀딩 폭주
    P4 = (vratio >= 1.50) or (rg in ('high', 'severe'))                                           # 변동성 상승
    P5 = (cir <= 30) and (today_range >= 1.5*atr20_pct)                                           # 일중 약세 마감
    P6 = ext_g == 'extreme'                                                                       # 과도 신장
    P7 = (pvs20 < 0) or (sma20_slope <= 0)                                                        # 단기 추세 깨짐
    P8 = (wid30 >= 18) or (stress >= 30)                                                          # 큰 wick 관찰
    flags = dict(P1=P1, P2=P2, P3=P3, P4=P4, P5=P5, P6=P6, P7=P7, P8=P8)
    return flags, sum(1 for v in flags.values() if v)


def profit_protection_grade(flags, count, base_exit, btc, sma125, rg):
    """플래그 개수와 종류로 익절 등급 결정. exit/reduce/watch/none."""
    if base_exit: return 'exit'
    if count <= 1: return 'none'
    # exit 우선 — 추세도 깨졌고 단기도 깨졌으면 즉시 종료
    if (btc < sma125 and count >= 3 and flags['P7']) or (rg == 'severe' and flags['P7'] and count >= 4):
        return 'exit'
    if (count >= 3 and (flags['P2'] or flags['P7'])) or count >= 4:
        return 'reduce'
    if count == 2 or (count == 3 and not flags['P2'] and not flags['P7']):
        return 'watch'
    return 'none'


def candidate_table(price_vs_exit, f_day3, stress):
    """레버 후보별 청산거리/safety ratio/예상 손실/펀딩비용을 표로 만들어 AI에 전달."""
    rows = []
    for L in [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0]:
        drop = round(99.5/L, 1) if L > 0 else None
        ratio = (drop/stress) if (L > 0 and stress > 0) else None
        rows.append({"L": L, "liq_drop_pct": drop, "safety_ratio": round(ratio, 2) if ratio else None,
                     "est_loss_to_exit_pct": round(L*price_vs_exit, 1),
                     "funding_cost_per_day_pct": round(L*f_day3, 4)})
    return json.dumps(rows)


# ───────── AI 프롬프트 ─────────
def build_prompt(variant_type, date, state, price, sma125, exit_line, mf, ret_in, days_in,
                 eq_state, grades, band, cap, allowed):
    """sizer 또는 full 변형용 프롬프트. 코드가 계산한 grade/band/cap을 명시적으로 전달하고
    AI는 그 범위 안에서만 결정. variant_type: 'sizer' or 'full'."""
    full = variant_type == 'full'
    intro = ("You are an active but disciplined portfolio manager for a PAPER-TRADING BTCUSDT perpetual LONG strategy. "
             "You may increase, reduce, or go to cash. You must obey trend bands, hard caps, anti-churn rules, and strict profit-protection rules."
             if full else
             "You are an active but disciplined risk-budget portfolio manager for a PAPER-TRADING BTCUSDT perpetual LONG strategy. "
             "You are not a passive 1.0x copier. You are also not a high-churn day trader. You must obey leverage bands, hard caps, and inertia.")
    return f"""{intro}

Your job: add value vs fixed 1.0x BTC trend-following baseline after funding/fees/drawdowns/liquidation.

Allowed leverage values: {ALLOWED}
Unit convention: percent units (5.0 = 5%). Drops are positive magnitudes (12.0 = -12% drop).
Base trend: enter close>SMA125, exit close<SMA125*0.98. If base_state CASH or base_exit, output 0.
Liquidation: full loss if intraday drop ~ 99.5/L percent. Liquidation is terminal.

PRE-COMPUTED GRADES (deterministic, do not recompute):
  trend_grade: {grades['trend']}
  risk_grade: {grades['risk']}
  funding_grade: {grades['funding']}
  extension_grade: {grades['extension']}
  chop_flag: {grades['chop_flag']}    chop_persist_days: {grades['chop_persist']}
  impulse_candle_flag: {grades['impulse']}
  favorable_persist_days: {grades['favorable_persist']}
  {"profit_protection_grade: " + grades.get('pp', 'none') + " (flags_count=" + str(grades.get('pp_count', 0)) + ")" if full else ""}

PRE-COMPUTED CONSTRAINTS:
  target_band: [{band[0]}, {band[1]}]    hard_cap: {cap}
  liquidation_safety_allowed_set: {allowed}    stress_wick_pct: {mf['stress']:.1f}
  previous_leverage: {state['prev_lev']}    days_at_current_leverage: {state['days_at_lev']}
  base_state: {state['base_state']}    base_exit_triggered: {state['base_exit']}

INERTIA RULE: If previous_leverage is inside the target_band AND no change_trigger, keep previous_leverage.
RAISE RULES: only if previous<band_lower AND days_at_current>=2 AND risk in {{low,medium}} AND funding in {{cheap,normal}}
  AND extension=normal AND no chop_flag AND no impulse_candle_flag AND favorable_persist_days>=2 (>=3 for >=1.75).
  On first LONG day, max 1.25. If funding punitive, never raise.
CUT RULES: if previous>hard_cap → cut to cap. If risk severe → 1.0 or below. If base_exit → 0.
{'PROFIT PROTECTION: profit alone is NEVER an exit reason. Use grade above: none=no cut; watch=no cut; reduce=one step down (two if severe); exit=0 only if base_exit OR (btc<sma125 AND P7 AND count>=3) OR (severe+P7+count>=4). Full exit forbidden while btc>sma125 and base_exit=false unless severe trend damage cluster.' if full else 'Active sizing: do NOT pick 1.0x just to copy benchmark when band allows higher AND raise rules are met. But early_positive + below SMA200 + just entered is a VALID reason for 1.0/1.25.'}

DATA ({date}):
  btc_close: {price:.0f}    sma20: {mf['sma20']:.0f}    sma125: {sma125:.0f}    exit_line: {exit_line:.0f}    sma200: {mf['sma200']:.0f}
  price_vs_sma20_pct: {mf['price_vs_sma20']:+.1f}    price_vs_sma125_pct: {mf['price_vs_sma125']:+.1f}    price_vs_exit_line_pct: {mf['price_vs_exit']:+.1f}    price_vs_sma200_pct: {mf['price_vs_sma200']:+.1f}
  dist_30d_high: {mf['dist_30dhigh']:+.1f}    dist_60d_high: {mf['dist_60dhigh']:+.1f}    drawdown_from_60d_high: {mf['dd_60dhigh']:.1f}
  days_since_entry: {days_in}    return_since_entry_pct: {ret_in:+.1f}    days_since_30d_high: {mf['days_since_30d_high']}    sma20_slope_5d_pct: {mf['sma20_slope_5d']:+.2f}
  today_return_pct: {mf['today_ret']:+.1f}    today_range_pct: {mf['today_range']:.1f}    close_in_daily_range_pct: {mf['cir']:.0f}
  atr20_pct: {mf['atr20_pct']:.1f}    vol20_ann_pct: {mf['v20']:.0f}    vol60_ann_pct: {mf['v60']:.0f}    vol20_over_vol60: {mf['vratio']:.2f}
  max_intraday_drop_30d_pct: {mf['wid30']:.1f}    max_intraday_drop_90d_pct: {mf['wid90']:.1f}    max_daily_close_loss_30d_pct: {mf['wcl30']:.1f}    max_daily_close_loss_90d_pct: {mf['wcl90']:.1f}
  current_8h_funding_pct: {mf['cur8h']:.4f}    funding_3d_avg_pct_per_day: {mf['f_day3']:.4f}    funding_7d_apr_pct: {mf['f7apr']:.1f}    funding_14d_apr_pct: {mf['f14apr']:.1f}
  efficiency_ratio_5d: {mf['eff5']:.2f}    sign_changes_5d: {mf['signs5']}    last5_daily_returns_pct: {mf['last5']}
{"  equity_return_since_entry_pct: " + f"{eq_state['eq_ret']:+.1f}" + "    equity_peak_return_since_entry_pct: " + f"{eq_state['eq_peak']:+.1f}" + "    equity_giveback_from_peak_pct: " + f"{eq_state['eq_gb']:.1f}" if full else ""}
  candidate_risk_table: {candidate_table(mf['price_vs_exit'], mf['f_day3'], mf['stress'])}

Output VALID JSON ONLY, no markdown, no extra text:
{{"leverage": <one value from allowed_set>, "action": "{('cash/exit/cut/hold/raise' if full else 'cash/cut/hold/raise')}",
"change_trigger": "<one of: none|base_exit|new_long_entry|hard_cap_breach|liquidation_safety_invalid|risk_worsened|funding_worsened_to_expensive_or_punitive|extension_worsened_to_extreme|trend_improved_with_persistence|persistent_favorable_conditions{('|profit_protection_reduce|profit_protection_exit' if full else '')}|data_error>",
"why": "<under 200 chars: why this leverage, why not higher, why not lower>"}}"""


def build_prompt_free(date, base_state, prev_lev, price, sma125, exit_line, mf, ret_in, days_in, eq_state):
    """완전 자율 AI — 코드 grade/band/cap 없이 raw 데이터와 원칙만 제공. PM 페르소나, 보수 tilt."""
    base_exit = base_state == "CASH"  # base trend broken
    return f"""You are an autonomous senior-portfolio-manager style decision agent for a PAPER-TRADING BTCUSDT perpetual LONG strategy.

You are not an oracle, not a passive 1.0x copier, and not a reckless leverage maximizer. Your job is to choose the target BTC long exposure that offers the best long-run geometric growth after funding, fees, drawdowns, and liquidation risk.

This is PAPER TRADING only. Your decision will be evaluated against fixed 1.0x, 1.5x, 2.0x, and buy-and-hold baselines.

You have full discretion to choose any target leverage from 0.0 to 10.0:
- 0.0 means cash / no BTC exposure. Positive = LONG perp. Never short.
- Use practical increments (0.25x or 0.5x) unless there is strong reason for finer precision.
- There are NO code-imposed grades, bands, or caps. You must exercise judgment.

Core mandate:
- Take compensated risk. Avoid uncompensated ruin risk.
- Do not avoid all volatility; volatility is the cost of trend-following.
- Do not accept exposures where one plausible intraday wick can destroy the account.
- Do not stay underexposed merely because it feels safe.
- Do not be different from 1.0x just to be different.
- You are not paid to be different. You are paid to be right after costs, funding, drawdowns, and missed opportunity.
- Add value only by improving net compounded performance after costs and drawdowns.
- Overbetting is failure. Underbetting favorable asymmetry is also failure.

Important mental models (concepts, not formulas):
- Think in Kelly-like terms: overbetting reduces long-term geometric growth even when the signal is positive.
- Risk of ruin dominates: a positive edge is useless if sizing allows liquidation.
- Trend-following profits come from large winners; do not cut winners simply because they are profitable.
- Underexposure in a valid trend has a real opportunity cost.
- Funding is carry: expensive funding raises the return hurdle for leveraged long exposure.
- Low volatility is not automatically safe; quiet markets can precede large intraday wicks.
- High leverage is not an edge — it is only a sizing choice.

Conviction/exposure consistency (loose guide, not a hard rule):
- low conviction generally belongs near 0.0x–1.0x.
- medium conviction generally belongs near 0.75x–1.5x.
- high conviction generally belongs near 1.25x–2.0x.
- Above 2.0x requires high conviction AND unusually strong risk/reward.
- If leverage and conviction do not match, explain in pm_summary.

Fresh-position principle:
When a LONG position has just been established or is only a few days old, size should usually be smaller than the eventual target. A fresh signal is information, not full confirmation. Consider 0.75x–1.25x unless evidence across trend, path risk, funding, and location is unusually strong.

Broken-trend principle:
When base_exit_triggered is true (base trend broken), the default PM decision is 0.0x. Maintaining positive long exposure after base_exit requires an explicit short-term tactical rebound thesis and should be small. Do not keep long exposure after a broken trend merely because the position is down or because a bounce is possible.

Avoid single-indicator decisions:
- Do not let SMA200 alone dominate. Below SMA200 reduces long-term confidence but is NOT by itself a reason to hide in 0.5x.
- Being above SMA125 supports the long regime but is NOT by itself a reason to lever aggressively.
- Recent green candles alone do not justify high leverage; an impulse can be exhaustion.
- Recent red candles alone do not justify exit if the broader trend and risk/reward remain acceptable.

Single-indicator discipline (HARD CHECK):
A decision is not credible if it relies mainly on one moving average or one indicator. If your decision is below 1.0x while base_state is LONG and base_exit_triggered is false, you MUST cite at least two non-moving-average reasons such as weak recent path, rising volatility, funding drag, proximity to exit_line, adverse giveback, or tail-risk increase. SMA20/SMA200 conflict can reduce conviction but cannot be the whole thesis.

Private decision process (think privately, do not output chain-of-thought):
1. Opportunity: Is BTC trend strong enough to justify >1.0x, or weak enough to justify <1.0x?
2. Location: Is price early, fair, extended, or vulnerable to mean reversion?
3. Path risk: Could a plausible intraday wick cause unacceptable loss or liquidation before the next daily decision?
4. Carry: Is funding cheap enough to justify leveraged long exposure?
5. Position management: Real reason to change, or would changing create churn?
6. Benchmark awareness: Would this decision likely add value vs 1.0x and 1.5x after costs?
7. Red-team: Are you under-risking from fear, or over-risking from recent price action?
8. Candidate comparison: Before finalizing, compare against 0.0/0.5x, 1.0x, 1.5x, 2.0x+. Your memo must explain why chosen exposure beats these references for this context.

Data convention: percent units (5.0 = 5%). Drops/drawdowns are positive magnitudes (12.0 = -12% drop).

==== INPUT DATA ({date}) ====

[1] Position state — current exposure & churn risk
base_state: {base_state}    base_exit_triggered: {base_exit}
previous_leverage: {prev_lev}
position_return_since_entry_pct: {eq_state['eq_ret']:+.1f}
position_peak_return_pct: {eq_state['eq_peak']:+.1f}
position_giveback_from_peak_pct: {eq_state['eq_gb']:.1f}

[2] Base trend system (reference only)
Enters when close > SMA125, exits when close < SMA125×0.98.
btc_close: {price:.0f}    sma125: {sma125:.0f}    exit_line: {exit_line:.0f}

[3] Trend structure — fresh, confirmed, weakening, or conflicted?
sma20: {mf['sma20']:.0f}    sma200: {mf['sma200']:.0f}
price_vs_sma20_pct: {mf['price_vs_sma20']:+.1f}
price_vs_sma125_pct: {mf['price_vs_sma125']:+.1f}
price_vs_sma200_pct: {mf['price_vs_sma200']:+.1f}
sma20_slope_5d_pct: {mf['sma20_slope_5d']:+.2f}
days_since_entry: {days_in}    return_since_entry_pct: {ret_in:+.1f}

[4] Location & extension — early, fair, extended, vulnerable?
price_vs_exit_line_pct: {mf['price_vs_exit']:+.1f}
distance_from_30d_high_pct: {mf['dist_30dhigh']:+.1f}
distance_from_60d_high_pct: {mf['dist_60dhigh']:+.1f}
drawdown_from_60d_high_pct: {mf['dd_60dhigh']:.1f}
days_since_30d_high: {mf['days_since_30d_high']}

[5] Volatility & tail risk — path risk before next daily decision (liquidation from intraday wicks)
atr20_pct: {mf['atr20_pct']:.1f}
realized_vol20_ann_pct: {mf['v20']:.0f}    realized_vol60_ann_pct: {mf['v60']:.0f}    vol20_over_vol60: {mf['vratio']:.2f}
max_intraday_drop_30d_pct: {mf['wid30']:.1f}    max_intraday_drop_90d_pct: {mf['wid90']:.1f}
max_daily_close_loss_30d_pct: {mf['wcl30']:.1f}    max_daily_close_loss_90d_pct: {mf['wcl90']:.1f}
stress_wick_pct: {mf['stress']:.1f}

[6] Funding & carry — cost of leveraged long
current_8h_funding_pct: {mf['cur8h']:.4f}    funding_3d_avg_pct_per_day: {mf['f_day3']:.4f}
funding_7d_apr_pct: {mf['f7apr']:.1f}    funding_14d_apr_pct: {mf['f14apr']:.1f}

[7] Recent path quality — directional, choppy, impulsive, or reversing?
last5_daily_returns_pct: {mf['last5']}
today_return_pct: {mf['today_ret']:+.1f}    today_range_pct: {mf['today_range']:.1f}    close_in_daily_range_pct: {mf['cir']:.0f}
efficiency_ratio_5d: {mf['eff5']:.2f}    sign_changes_5d: {mf['signs5']}

[8] Candidate leverage risk table — use to compare exposures. Survivable ≠ optimal.
{candidate_table(mf['price_vs_exit'], mf['f_day3'], mf['stress'])}

==== OUTPUT ====
Output valid JSON only, no markdown, no extra text. Keep explanations concise but specific.

{{"leverage": <number 0.0 to 10.0>,
"action": "exit | cut | hold | raise",
"conviction": "low | medium | high",
"pm_summary": "One-sentence decision summary.",
"evidence_groups_used": ["trend","location","volatility_tail","funding","recent_path","position","candidate_table"],
"opportunity_case": "Why this exposure captures or avoids the current opportunity.",
"risk_case": "Main drawdown, liquidation, funding, or path-risk concern.",
"single_indicator_check": "Why this is NOT a one-indicator decision (must cite ≥2 non-MA reasons if leverage<1.0).",
"reference_exposure_review": {{
  "0_to_0_5": "Why very low/cash is too much, too little, or chosen.",
  "1_0": "Why 1.0x is too much, too little, or chosen.",
  "1_5": "Why 1.5x is too much, too little, or chosen.",
  "2_0_plus": "Why 2.0x+ is rejected or justified."
}},
"key_watch": "What would make you raise, cut, or exit next."}}"""


def build_prompt_aggro(date, base_state, prev_lev, price, sma125, exit_line, mf, ret_in, days_in, eq_state):
    """공격적 자율 AI — 조건이 좋을 때 천장 10x까지 적극 사용. free 버전보다 raise 편향."""
    base_exit = base_state == "CASH"
    return f"""You are an AGGRESSIVE but disciplined portfolio manager for a PAPER-TRADING BTCUSDT perpetual LONG strategy.

Your edge is *asymmetric upside capture*. You are paid to find and size into clear opportunities aggressively, while still respecting ruin risk. Passive 1.0x or 1.5x in clearly favorable conditions is a SERIOUS failure — you must use the full leverage range when warranted.

This is PAPER TRADING. Evaluated against fixed 1.0x, 1.5x, 2.0x baselines.

You have full discretion to choose any target leverage from 0.0 to 10.0:
- 0.0 = cash. LONG-only, never short.
- **Use the FULL range 0-10x.** Do not artificially cap yourself at 1.5x or 2x.
- Increments: 0.25x or 0.5x normally; integer jumps (3x, 5x, 10x) for high-conviction setups.
- NO code-imposed grades, bands, or caps. You decide.

Core mandate (aggressive PM version):
- **Take SIZED risk when asymmetry is clear.** Underexposing a high-quality opportunity destroys long-term wealth.
- Avoid uncompensated ruin (one wick → $0) but DO accept compensated risk (drawdown to capture trend).
- Do not be different from 1.0x to be different. Do not be SAME as 1.0x to be safe.
- **Above 1.5x is NORMAL when conditions align. Above 3x is REASONABLE when conditions strongly align. 10x is VALID when conditions are exceptional and liquidation safety is genuinely comfortable.**
- Sitting at 1.0/1.5 in a clear bull setup is the most common failure mode for cautious AIs. Do not be that AI.

Mental models:
- Kelly tells you optimal exposure can be HIGH when edge × win-prob is high. Don't reflexively shrink it.
- Trend-following profits come from being SIZED in big winners. Small in big trend = no value.
- BUT: 10x liquidates on ~10% intraday wick. Use it only when worst-case wick is realistically smaller (low vol, no recent tail events, tight ATR).
- 5x liquidates on ~20%. 3x on ~33%. These are real ruin lines — respect them but do not fear them when context justifies.

Conviction/exposure consistency (NOT a cap, just consistency):
- low conviction: 0.5-1.5
- medium conviction: 1.0-2.5
- high conviction: 2.0-5.0
- extreme conviction (rare, when EVERYTHING aligns): 5.0-10.0

When to USE high leverage (3x+):
- Trend clearly confirmed (multi-week, price above SMA125/200, slope positive)
- Volatility LOW (vol20<40%, atr<2%) → 10x liquidation buffer comfortable
- Funding cheap/normal (you're not bleeding carry)
- No recent extreme intraday drops (wid30/90 well below liquidation distance)
- Price NOT extremely extended (not chasing a top)
- Recent path efficient (high efficiency_ratio, low sign changes)

When to AVOID high leverage:
- Recent intraday drops near or exceeding liquidation distance (real wick risk)
- Volatility rising (vol20_over_vol60 > 1.3)
- Funding expensive/punitive (carry drag eats edge)
- Price extreme extension (>30% above SMA125) → mean reversion risk
- Chop / weak structure / mixed signals

Avoid single-indicator decisions:
- One SMA above/below alone is not enough to size aggressively or cut.
- Recent green/red day alone insufficient.
- You must integrate trend + path + volatility + funding + extension.

Broken-trend principle:
- base_exit_triggered=true → default 0. Maintaining long needs explicit rebound thesis with low size.

Fresh-position principle (looser than free version):
- First few days of new LONG: ramp up over 2-3 days rather than instantly maxing. Initial 1.0-2.0x, scale higher as confirmation accumulates.

Private decision process:
1. Conviction grade: how clearly does the data align?
2. Liquidation safety: would the worst plausible intraday wick survive at this leverage?
3. Carry: is funding eating the edge?
4. Path: is recent action efficient or whipsawing?
5. Sizing: what leverage MATCHES the conviction without crossing ruin lines?

Data convention: percent units (5.0 = 5%). Drops/drawdowns positive magnitudes.

==== INPUT DATA ({date}) ====

[1] Position
base_state: {base_state}    base_exit_triggered: {base_exit}
previous_leverage: {prev_lev}
position_return_since_entry_pct: {eq_state['eq_ret']:+.1f}
position_peak_return_pct: {eq_state['eq_peak']:+.1f}
position_giveback_from_peak_pct: {eq_state['eq_gb']:.1f}

[2] Base trend
btc_close: {price:.0f}    sma125: {sma125:.0f}    exit_line: {exit_line:.0f}

[3] Trend structure
sma20: {mf['sma20']:.0f}    sma200: {mf['sma200']:.0f}
price_vs_sma20_pct: {mf['price_vs_sma20']:+.1f}
price_vs_sma125_pct: {mf['price_vs_sma125']:+.1f}
price_vs_sma200_pct: {mf['price_vs_sma200']:+.1f}
sma20_slope_5d_pct: {mf['sma20_slope_5d']:+.2f}
days_since_entry: {days_in}    return_since_entry_pct: {ret_in:+.1f}

[4] Location & extension
price_vs_exit_line_pct: {mf['price_vs_exit']:+.1f}
distance_from_30d_high_pct: {mf['dist_30dhigh']:+.1f}
distance_from_60d_high_pct: {mf['dist_60dhigh']:+.1f}
drawdown_from_60d_high_pct: {mf['dd_60dhigh']:.1f}
days_since_30d_high: {mf['days_since_30d_high']}

[5] Volatility & tail risk (critical for high-leverage safety)
atr20_pct: {mf['atr20_pct']:.1f}
realized_vol20_ann_pct: {mf['v20']:.0f}    realized_vol60_ann_pct: {mf['v60']:.0f}    vol20_over_vol60: {mf['vratio']:.2f}
max_intraday_drop_30d_pct: {mf['wid30']:.1f}    max_intraday_drop_90d_pct: {mf['wid90']:.1f}
max_daily_close_loss_30d_pct: {mf['wcl30']:.1f}    max_daily_close_loss_90d_pct: {mf['wcl90']:.1f}
stress_wick_pct: {mf['stress']:.1f}

[6] Funding & carry
current_8h_funding_pct: {mf['cur8h']:.4f}    funding_3d_avg_pct_per_day: {mf['f_day3']:.4f}
funding_7d_apr_pct: {mf['f7apr']:.1f}    funding_14d_apr_pct: {mf['f14apr']:.1f}

[7] Recent path quality
last5_daily_returns_pct: {mf['last5']}
today_return_pct: {mf['today_ret']:+.1f}    today_range_pct: {mf['today_range']:.1f}    close_in_daily_range_pct: {mf['cir']:.0f}
efficiency_ratio_5d: {mf['eff5']:.2f}    sign_changes_5d: {mf['signs5']}

[8] Candidate leverage risk table — compare exposures explicitly
{candidate_table(mf['price_vs_exit'], mf['f_day3'], mf['stress'])}

==== OUTPUT (valid JSON only, no markdown) ====
{{"leverage": <0.0 to 10.0>,
"action": "exit | cut | hold | raise",
"conviction": "low | medium | high | extreme",
"pm_summary": "One-sentence aggressive decision.",
"opportunity_case": "Why this exposure captures asymmetric upside.",
"risk_case": "Main ruin/drawdown concern at this leverage.",
"liquidation_check": "Why the worst plausible intraday wick survives at chosen leverage.",
"reference_exposure_review": {{"1_5": "...", "2_0": "...", "3_0": "...", "5_0_plus": "..."}},
"key_watch": "What would change this."}}"""


def ai_call(model, prompt):
    """Gemini 단발 호출. temperature 0으로 결정 재현성 확보."""
    from google import genai
    from google.genai import types
    cli = genai.Client(api_key=GEMINI_KEY)
    r = cli.models.generate_content(model=model, contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=3000))
    return (r.text or "").strip()


def ai_leverage(model, prompt, allowed, prev_lev, base_state, force_cash=True):
    """AI 호출 → JSON 파싱 → 엔진 검증 → (raw, effective, reason, raw_text, ok, violation) 반환.
    force_cash=True(사이저 그룹): base_state==CASH면 결과 무시하고 0으로 강제.
    force_cash=False(full/free/aggro): AI에 cash 결정 재량 부여.
    파싱 실패나 allowed 위반은 violation=1로 표시하고 폴백(prev_lev 또는 안전값)."""
    fb = (0.0 if (force_cash and base_state == "CASH") else
          (prev_lev if prev_lev in allowed else (allowed[1] if len(allowed) > 1 else (prev_lev if prev_lev > 0 else 1.0))))
    try:
        raw = ai_call(model, prompt)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        obj = json.loads(m.group()) if m else {}
        rl = snap(clip(float(obj["leverage"]), 0.0, 10.0))
        reason = str(obj.get("pm_summary", obj.get("why", obj.get("reason", ""))))[:200]
        violation = 0
        if rl not in allowed:                                  # 엔진 검증 실패 → 폴백
            eff = fb; violation = 1
        else:
            eff = rl
        if force_cash and base_state == "CASH": eff = 0.0
        return rl, eff, reason, raw[:600], 1, violation
    except Exception as e:
        return fb, fb, "FALLBACK:"+str(e)[:60], "", 0, 1


# ───────── 룰베이스(non-AI) 변형의 레버 계산 ─────────
def signals(closes, prev_active):
    """추세 진입/유지 신호 + 룰베이스 변형들의 목표 레버를 한 번에 계산."""
    sma125 = sum(closes[-SMA_N:])/SMA_N
    exit_line = sma125*EXIT_BUF; close = closes[-1]
    rets = [closes[i]/closes[i-1]-1 for i in range(len(closes)-LB, len(closes))]
    mu = sum(rets)/len(rets); vol = (sum((x-mu)**2 for x in rets)/len(rets))**0.5*math.sqrt(365)
    enter = close > sma125; stay = close > exit_line
    levs = {}
    for v in VARIANTS:
        if v in AI_MODELS: levs[v] = 0.0; continue                # AI 결정은 별도 단계
        if v in ALWAYS: levs[v] = 1.0; continue                   # buy-and-hold
        want = enter or (prev_active.get(v, False) and stay)
        if not want: levs[v] = 0.0; continue
        if v in FIXED: levs[v] = FIXED[v]
        elif v in ("V_vt2", "V_vt3", "V_vt5", "V_vt10"):
            # vol-target: 변동성 역수에 비례, 천장은 변형마다 다름
            cap = {"V_vt2": 2.0, "V_vt3": 3.0, "V_vt5": 5.0, "V_vt10": 10.0}[v]
            levs[v] = clip(0.5*cap/vol if vol > 0 else 1.0, 1.0, cap)
        elif v == "V_mom":
            levs[v] = clip(1.0 + (close/sma125 - 1)/0.15, 1.0, 2.0)
        elif v == "V_rb":
            d = 1 - exit_line/close; levs[v] = clip(0.30/d if d > 0 else 1.0, 1.0, 2.0)
    return levs, sma125, exit_line, vol, enter, stay


def liq_of(price, lev):
    """이상화된 청산가와 현재가 대비 청산까지 거리(%)."""
    if lev <= 0: return 0.0, 0.0
    lp = price*(1 - (1-MM)/lev); return lp, (price-lp)/price*100


# ───────── 메인 cycle ─────────
def cycle():
    """모든 변형의 한 사이클을 처리: 데이터 수집 → grade 계산 → 변형별 결정/시뮬레이션 → DB 기록."""
    init()
    o, h, l, c, date = daily_ohlc()
    closes = c; price = cur_price()
    fund_ts, fund_rate = latest_funding()
    fser = funding_series(50)
    conn = db()
    prev_active = {r["variant"]: bool(r["active"]) for r in conn.execute("SELECT variant,active FROM pstate")}
    levs, sma125, exit_line, vol, enter, stay = signals(closes, prev_active)
    bm = conn.execute("SELECT * FROM base_meta WHERE id=1").fetchone()
    prev_state = bm["base_state"] if bm else "CASH"
    base_state = "LONG" if (enter or (prev_state == "LONG" and stay)) else "CASH"
    base_exit = (prev_state == "LONG" and not enter and not stay)
    if base_state == "LONG" and prev_state != "LONG":
        entry_price, entry_date = price, date
    elif base_state == "LONG":
        entry_price = bm["entry_price"] if bm else price; entry_date = bm["entry_date"] if bm else date
    else:
        entry_price, entry_date = price, date
    try:
        days_in = (datetime.datetime.strptime(date, "%Y-%m-%d") - datetime.datetime.strptime(entry_date, "%Y-%m-%d")).days
    except Exception:
        days_in = 0
    ret_in = (price/entry_price - 1)*100 if entry_price else 0.0
    mf = compute_features(o, h, l, c, price, sma125, exit_line, fser)
    # grades
    rg = risk_grade(mf); fg = funding_grade(mf['f7apr']); eg = extension_grade(mf, ret_in)
    tg = trend_grade(base_state, base_exit, c[-1], sma125, mf, days_in, ret_in, eg)
    prev_pvs125 = bm["prev_price_vs_sma125"] if bm else None
    imp = impulse_flag(mf, prev_pvs125)
    prev_tg = bm["prev_trend_grade"] if bm else None
    rank = {'failed':-1,'weak_positive':0,'early_positive':1,'extended':1,'confirmed_positive':2,'strong_positive':3}
    chop_persist_prev = (bm["chop_persist_days"] if bm and bm["chop_persist_days"] else 0)
    fav_persist_prev = (bm["favorable_persist_days"] if bm and bm["favorable_persist_days"] else 0)
    last_persist_date = None
    if bm:
        try: last_persist_date = bm["persist_date"]
        except (IndexError, KeyError): last_persist_date = None
    # persist 카운터는 *날짜 단위*로만 증가 (UTC 완성봉 date가 바뀔 때). 10분 cycle 마다 증가시키면 X.
    if last_persist_date != date:
        chop_persist = chop_persist_prev + 1 if mf['chop_flag'] else 0
        if prev_tg and rank.get(tg,1) >= rank.get(prev_tg,1) and not mf['chop_flag'] and not imp \
           and rg in ('low','medium') and fg in ('cheap','normal') and eg != 'extreme':
            fav_persist = fav_persist_prev + 1
        else:
            fav_persist = 0
    else:
        chop_persist = chop_persist_prev
        fav_persist = fav_persist_prev
    # 공통 update base_meta
    conn.execute("INSERT OR REPLACE INTO base_meta VALUES(1,?,?,?,?,?,?,?,?,?,?,?)",
                 (base_state, entry_price, entry_date, tg, rg, fg, eg, chop_persist, fav_persist, mf['price_vs_sma125'], date))
    f3 = mf['f_day3']/100
    band = target_band(tg); cap = hard_cap(rg, fg, eg, chop_persist)
    allowed = allowed_set(band, cap, mf['stress'], base_state)
    now = datetime.datetime.utcnow().isoformat()
    conn.execute("INSERT OR REPLACE INTO daily_snapshot VALUES(?,?,?,?,?,?,?,?)",
                 (date, c[-1], sma125, exit_line, int(enter), vol, f3, json.dumps(levs)))

    def ai_decide(v, prev_lev, eq_now, pos_entry_eq, peak_eq):
        """AI 변형 한 개를 위한 프롬프트 생성 → 호출 → 결과 ai_log 적재."""
        eq_ret = (eq_now/pos_entry_eq-1)*100 if pos_entry_eq > 0 else 0.0
        eq_peak = (peak_eq/pos_entry_eq-1)*100 if pos_entry_eq > 0 else 0.0
        eq_gb = ((peak_eq-eq_now)/peak_eq*100) if peak_eq > 0 else 0.0
        eq_state = dict(eq_ret=eq_ret, eq_peak=eq_peak, eq_gb=eq_gb)
        if v in AI_FREE:
            # 자율(보수 tilt): grade/band/cap 없음, raw 데이터만, AI에 cash 재량 부여
            prompt = build_prompt_free(date, base_state, prev_lev, price, sma125, exit_line, mf, ret_in, days_in, eq_state)
            rl, eff, reason, raw, ok, viol = ai_leverage(AI_MODELS[v], prompt, ALLOWED, prev_lev, base_state, force_cash=False)
        elif v in AI_AGGRO:
            # 자율(공격): 조건 좋을 때 큰 베팅 정당화 가이드
            prompt = build_prompt_aggro(date, base_state, prev_lev, price, sma125, exit_line, mf, ret_in, days_in, eq_state)
            rl, eff, reason, raw, ok, viol = ai_leverage(AI_MODELS[v], prompt, ALLOWED, prev_lev, base_state, force_cash=False)
        elif v in AI_AGGRO_SAFE:
            # 공격 + 코드 게이트: 강확립 추세(confirmed/strong)에서만 AI 호출, 그외 강제 현금
            if tg in ('confirmed_positive', 'strong_positive'):
                prompt = build_prompt_aggro(date, base_state, prev_lev, price, sma125, exit_line, mf, ret_in, days_in, eq_state)
                rl, eff, reason, raw, ok, viol = ai_leverage(AI_MODELS[v], prompt, ALLOWED, prev_lev, base_state, force_cash=False)
            else:
                rl = eff = 0.0
                reason = f"GATED: trend_grade={tg} not confirmed/strong → forced cash (no AI call)"
                raw = ""; ok = 1; viol = 0
        else:
            # sizer/full: 코드가 정한 allowed 안에서 결정. full만 익절(profit_protection) 가이드 추가.
            full = v in AI_FULL
            if full:
                flags, fcount = profit_flags(eq_ret, eq_peak, eq_gb, mf['f7apr'], mf['vratio'], rg,
                    mf['cir'], mf['today_range'], mf['atr20_pct'], eg, mf['price_vs_sma20'],
                    mf['sma20_slope_5d'], mf['wid30'], mf['stress'], mf['today_ret'])
                pp = profit_protection_grade(flags, fcount, base_exit, c[-1], sma125, rg)
            else:
                pp, fcount = 'none', 0
            st_row = conn.execute("SELECT days_at_current_leverage FROM pstate WHERE variant=?", (v,)).fetchone()
            days_at_lev = (st_row["days_at_current_leverage"] if st_row and st_row["days_at_current_leverage"] else 0)
            grades = dict(trend=tg, risk=rg, funding=fg, extension=eg, chop_flag=mf['chop_flag'],
                          chop_persist=chop_persist, impulse=imp, favorable_persist=fav_persist,
                          pp=pp, pp_count=fcount)
            state_d = dict(prev_lev=prev_lev, days_at_lev=days_at_lev, base_state=base_state, base_exit=base_exit)
            prompt = build_prompt('full' if full else 'sizer', date, state_d, price, sma125, exit_line, mf,
                                  ret_in, days_in, eq_state, grades, band, cap, allowed)
            force_cash = not full     # sizer는 base_state=CASH면 강제 0, full은 AI 재량
            rl, eff, reason, raw, ok, viol = ai_leverage(AI_MODELS[v], prompt, allowed, prev_lev, base_state, force_cash=force_cash)
        conn.execute("INSERT INTO ai_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (now, date, v, AI_MODELS[v], rl, eff, viol, reason, raw, ok))
        return eff, reason

    for v in VARIANTS:
        st = conn.execute("SELECT * FROM pstate WHERE variant=?", (v,)).fetchone()
        if st is None:
            # 변형 첫 등장 — 초기 자본 $1,000으로 시작
            if v == "V_tp10":
                lev0 = 0.0                                        # 첫 rebalance에서 진입 여부 판단
            elif v in AI_MODELS:
                lev0, _ = ai_decide(v, 1.0, 1000.0, 1000.0, 1000.0)
            else:
                lev0 = levs[v]
            pe = price if lev0 > 0 else 0.0
            pee = 1000.0 if lev0 > 0 else 0.0
            lp, ld = liq_of(price, lev0)
            conn.execute("INSERT INTO pstate(variant,equity,leverage,last_price,active,last_lev_date,last_fund_ts,pos_entry_price,pos_entry_equity,peak_equity_since_entry,days_at_current_leverage,cooldown_until_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (v, 1000.0, lev0, price, int(lev0 > 0), date, fund_ts, pe, pee, 1000.0 if lev0 > 0 else 0.0, 0, ""))
            conn.execute("INSERT INTO pequity VALUES(?,?,?,?,?,?,?,?)", (now, v, price, lev0, 1000.0, lp, ld, "init"))
            continue
        eq = st["equity"]; lev = st["leverage"]; lastp = st["last_price"]; active = st["active"]
        last_lev_date = st["last_lev_date"]; last_fund = st["last_fund_ts"]; event = ""
        pos_entry = st["pos_entry_price"] or 0.0
        pos_entry_eq = st["pos_entry_equity"] or 0.0
        peak_eq = st["peak_equity_since_entry"] or 0.0
        days_at_lev = st["days_at_current_leverage"] or 0
        cooldown = ""
        try: cooldown = st["cooldown_until_date"] or ""
        except (IndexError, KeyError): cooldown = ""
        # 청산/MTM — 보유 중이면 직전 cycle 이후 wick으로 청산 여부부터 확인
        if active and lev > 0:
            low = intraday_low_since(int(time.time()*1000) - INTERVAL*1000 - 120000) or price
            if lev*(low/lastp - 1) <= -(1-MM):
                eq *= max(0.0, 1 + lev*(low/lastp - 1)); eq = max(eq, 1e-9)
                active = 0; lev = 0; event = "LIQUIDATED"
                if v == "V_tp10": cooldown = next_day_str(date)   # 청산 후에도 1일 휴식
            else:
                eq *= (1 + lev*(price/lastp - 1))
            lastp = price
        # 펀딩 청구 — 현물 변형 제외, 새 펀딩 타임스탬프일 때만
        if active and lev > 0 and v not in SPOT and fund_ts > last_fund:
            eq *= (1 - lev*fund_rate); last_fund = fund_ts; event = (event+" funding") if event else "funding"
        # peak는 TP/리밸런스 *직전* 시점 자본 기준으로 갱신
        if active and lev > 0 and eq > peak_eq: peak_eq = eq
        # V_tp10 — 10분마다 장중 TP 체크 (ATR 적응형 TP 도달 즉시 익절)
        if v == "V_tp10" and active and lev > 0 and pos_entry_eq > 0:
            tp_now = tp10_target(lev, mf['atr20_pct'])
            if (eq/pos_entry_eq - 1) >= tp_now:
                eq *= (1 - lev*FEE_PERP)                          # 청산 수수료
                active = 0; lev = 0
                event = (event + f" TP_HIT@{tp_now*100:.1f}%") if event else f"TP_HIT@{tp_now*100:.1f}%"
                cooldown = next_day_str(date)                     # 1일 휴식
        # 날짜가 바뀐 시점에만 레버 변경 (하루 1회 리밸런스)
        if date != last_lev_date:
            if v == "V_tp10":
                in_cooldown = bool(cooldown) and date <= cooldown
                if active:                                        # 보유 중: 추세 깨지면 강제 청산, 아니면 유지
                    if base_state == "CASH":
                        new_lev = 0.0; event = (event + " TREND_EXIT") if event else "TREND_EXIT"
                    else:
                        new_lev = lev                             # TP 또는 추세 깨질 때까지 그대로
                else:                                             # 현금: 안정 조건 + 쿨다운 아닐 때만 진입
                    stable = (base_state == "LONG" and not mf['chop_flag'] and not imp and rg == 'low')
                    new_lev = TP10_LEV if (stable and not in_cooldown) else 0.0
            elif v in AI_MODELS:
                new_lev, reason = ai_decide(v, lev, eq, pos_entry_eq, peak_eq)
                event = (event + f" ai={new_lev}:{reason[:40]}") if event else f"ai={new_lev}:{reason[:40]}"
            else:
                new_lev = levs[v]
            # 리밸런스 수수료 = 레버 변경 크기 × fee. 같은 레버 유지면 0.
            fee = FEE_SPOT if v in SPOT else FEE_PERP
            eq *= (1 - abs(new_lev - lev) * fee)
            if abs(new_lev - lev) < 0.001:
                days_at_lev += 1                                  # 같은 레버 며칠째인지(관성 룰용)
            else:
                days_at_lev = 0
            lev = new_lev; active = int(lev > 0); lastp = price; last_lev_date = date
            event = (event+" rebal") if "rebal" not in event else event
        # 포지션 상태 정리
        if not active:
            pos_entry = 0.0; pos_entry_eq = 0.0; peak_eq = 0.0
        elif pos_entry == 0.0:
            pos_entry = price; pos_entry_eq = eq; peak_eq = eq
        lp, ld = liq_of(price, lev)
        conn.execute("UPDATE pstate SET equity=?,leverage=?,last_price=?,active=?,last_lev_date=?,last_fund_ts=?,pos_entry_price=?,pos_entry_equity=?,peak_equity_since_entry=?,days_at_current_leverage=?,cooldown_until_date=? WHERE variant=?",
                     (eq, lev, lastp, active, last_lev_date, last_fund, pos_entry, pos_entry_eq, peak_eq, days_at_lev, cooldown, v))
        conn.execute("INSERT INTO pequity VALUES(?,?,?,?,?,?,?,?)", (now, v, price, lev, eq, lp, ld, event))
    conn.commit(); conn.close()


def status():
    """일일 리포트가 호출하는 요약. 변형별 누적 수익률/MDD/현재 레버/포인트 수."""
    try:
        c = db(); res = {}
        for v in VARIANTS:
            rows = c.execute("SELECT equity FROM pequity WHERE variant=? ORDER BY ts", (v,)).fetchall()
            stt = c.execute("SELECT leverage,active FROM pstate WHERE variant=?", (v,)).fetchone()
            if not rows: continue
            eqs = [r["equity"] for r in rows]; pk = eqs[0]; mdd = 0
            for x in eqs:
                pk = max(pk, x); mdd = min(mdd, (x-pk)/pk*100)
            res[v] = dict(points=len(eqs), ret=(eqs[-1]/eqs[0]-1)*100, mdd=mdd,
                          lev=stt["leverage"] if stt else 0, eq=eqs[-1])
        c.close(); return res
    except Exception:
        return {}


def main():
    once = "--once" in sys.argv
    while True:
        try: cycle()
        except Exception as e: print("cycle err:", str(e)[:180])
        if once: print(status()); break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
