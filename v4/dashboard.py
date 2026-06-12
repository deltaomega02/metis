#!/usr/bin/env python3
"""
운영 통합 대시보드 — 모든 봇·서비스·페이퍼 시뮬을 한 페이지에 표시.

자동 갱신 없음 (F5로만 새로고침). 의존성 없이 표준 라이브러리의 http.server를 그대로
사용해 ~20MB RAM으로 동작. 기본 포트 8501.

표시 항목:
  - HERO: 총 자산 / XRP 라이브 상태 / BTC 가격 + 추세 / 펀딩 + carry 상태
  - 실전 운영 3트랙: BTC 추세봇 / Carry / XRP 라이브 MR
  - systemd 서비스 상태
  - 페이퍼 변형 그룹별 카드 (수익률/MDD/레버/청산거리)
  - AI 호출 로그 + grade 표시
  - XRP MR 페이퍼 상세
"""
import os, sqlite3, datetime, subprocess, json, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen
from pybit.unified_trading import HTTP


def get_usdkrw():
    """USD→KRW 환율. 실패시 fallback 1370."""
    try:
        with urlopen("https://api.exchangerate-api.com/v4/latest/USD", timeout=3) as r:
            return float(json.loads(r.read())["rates"]["KRW"])
    except Exception:
        return 1370.0


def krw_str(usd, rate):
    """USD 금액 → '₩X,XXX' (천단위 콤마)."""
    krw = usd * rate
    return f"₩{krw:+,.0f}".replace("+-", "-")

DIR = os.path.dirname(os.path.abspath(__file__))
TREND_DB = os.path.join(DIR, "metis_v4.db")
CARRY_DB = os.path.join(DIR, "metis_v5_carry.db")
PAPER_DB = os.path.join(DIR, "paper_lev.db")
XRP_DB = os.path.join(DIR, "paper_xrp.db")
XRP_LIVE_DB = os.path.join(DIR, "xrp_live.db")

# 변형별 설명 (기술 한 줄 / 쉬운 한 줄). 카드의 details에 표시됨.
VARIANT_DESC = {
    "P0_1x": ("BTC 현물 1x. close>SMA125 진입, close<SMA125×0.98 청산. 펀딩 없음. risk-adjusted 기준선.",
              "그냥 *위면 사고 아래면 팔기* 1배짜리. 모든 변형 비교의 기준선."),
    "P1_1.5x": ("1.5x perp 추세. 펀딩비 발생. 유일한 실전 후보 레버 변형.",
                "1.5배로 거는 것. 8년 백테스트 수익 2.6배지만 낙폭도 -62%. 견딜 수 있으면 실전 가능."),
    "S1_buyhold": ("BTC 현물 1x 항상 보유. 추세필터 없음. buy-and-hold benchmark.",
                   "비트 그냥 사놓고 안 팔기. 추세필터가 진짜 가치 있나 비교용. 8년 MDD -77%."),
    "V_vt2": ("vol-target, L = clip(0.5×2/vol, 1, 2). 천장 2x.",
              "변동성 보고 자동 조절, 천장 2배. 잠잠하면 2x 풀로, 출렁이면 1x로 줄임."),
    "V_vt5": ("vol-target, L = clip(0.5×5/vol, 1, 5). 천장 5x. BTC에서 청산 위험구간(-20% wick).",
              "천장 5배. 평온할 땐 5x인데 BTC 장중 -20% 한 방이면 청산. 분기마다 그런 날 옴."),
    "V_vt10": ("vol-target, L = clip(0.5×10/vol, 1, 10). 천장 10x. 청산 -10% wick.",
               "천장 10배. *평온할 때 천재같다가 첫 -10% 급락에 $0*. 데모 + 교육용."),
    "V_ai_pro": ("Gemini 3.1 Pro 사이저. 추세룰 강제(CASH=0), grade/band/cap 코드 계산. force_cash=True.",
                 "AI(Pro)가 *코드가 정해준 범위 안에서만* 결정. CASH면 무조건 0. 룰 작동 테스트."),
    "V_ai_flash": ("V_ai_pro와 동일 프롬프트, Gemini 3.5 Flash 사용. Pro vs Flash 비용/품질 비교용.",
                   "위랑 똑같은데 더 싼 AI(Flash). 같은 결정 나오면 Flash 비용 압승."),
    "V_ai_full": ("Pro + 재량 익절(profit_protection 8 flags). 추세 안에서 단계감축/탈출 가능. winner-cut 가드 박힘.",
                  "AI가 사고파는 것까지 다 결정(룰은 있음). '이쯤 먹고 나오죠' 가능. 단 너무 일찍 안 자르게 가드."),
    "V_ai_pro_free": ("Gemini 3.1 Pro, 원칙 기반 가이드 프롬프트. grade/band/cap 코드 강제 X. raw 데이터만, force_cash=False.",
                      "AI(Pro)에게 *완전 위임*. 원칙만 알려주고 다 알아서. *진짜 AI 판단력* 테스트."),
    "V_ai_flash_free": ("V_ai_pro_free와 동일 가이드 프롬프트, Flash 사용.",
                        "위랑 같은데 Flash. 자율 환경에서 Pro vs Flash 진짜 차이 나는지 비교."),
    "V_ai_pro_aggro": ("Gemini 3.1 Pro, *공격적* 자율 프롬프트. 천장 10x 적극 사용. 조건 좋으면 3x/5x/10x 정당화 가이드.",
                       "AI(Pro)에게 *공격성* 부여 — 좋은 시장에서 1.5에 안 머물고 5x/10x까지 GO. 청산 회피 가드는 유지."),
    "V_ai_flash_aggro": ("V_ai_pro_aggro와 동일 공격 프롬프트, Flash 사용.",
                         "위랑 같은데 Flash. 공격 모드에서 Pro vs Flash 결정 차이 나는지 확인."),
    "V_ai_pro_aggro_safe": ("Pro 공격 + 시스템 게이트. trend_grade가 confirmed_positive/strong_positive일 때만 AI 호출, 그외 강제 현금(0x). 약세장 AI 호출 차단.",
                            "공격 AI인데 *코드가 약세장 잠금*. 추세 확실할 때만 AI 결정, 약세/혼란이면 무조건 현금. 백테스트에서 공격 AI 약세장 -60% 확인 후 추가."),
    "V_ai_flash_aggro_safe": ("V_ai_pro_aggro_safe와 동일 게이트, Flash 사용.",
                              "위랑 같은데 Flash. 게이트 환경에서 Pro vs Flash 차이 확인."),
    "V_tp10": ("10x 진입(LONG+chop X+impulse X+risk_low 안정조건). 적응형 TP=clip(lev×ATR%×0.5, 4%, 15%). 1일 쿨다운.",
               "10배 들어가서 ATR 기반 TP 먹고 하루 쉬기. 며칠 잘 보이다 첫 wick에 $0 가능성. 데모 + 교육용."),
}
s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))


def q(db, sql):
    try:
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        r = c.execute(sql).fetchall(); c.close(); return r
    except Exception as e:
        return [{"err": str(e)[:80]}]


def is_active(svc):
    try: return subprocess.check_output(["systemctl", "is-active", svc], text=True).strip() == "active"
    except Exception: return False


def free_mem():
    try:
        out = subprocess.check_output(["free", "-m"], text=True).splitlines()[1].split()
        return int(out[6])
    except Exception: return -1


def disk_free():
    try:
        out = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()[1].split()
        return out[3]
    except Exception: return "?"


def color(v):
    if v is None: return "mut"
    if v > 0: return "pos"
    if v < 0: return "neg"
    return "mut"


def gather():
    out = {}
    out["now_utc"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    out["now_kst"] = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        t = s.get_tickers(category="spot", symbol="BTCUSDT")["result"]["list"][0]
        out["btc_price"] = float(t["lastPrice"]); out["btc_chg24"] = float(t.get("price24hPcnt", 0))*100
    except Exception:
        out["btc_price"] = 0; out["btc_chg24"] = 0
    try:
        wb = s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
        coins = {c["coin"]: float(c.get("walletBalance", 0) or 0) for c in wb["coin"]}
        out["btc_held"] = coins.get("BTC", 0); out["usdt"] = coins.get("USDT", 0); out["usdc"] = coins.get("USDC", 0)
        out["borrow"] = sum(float(c.get("borrowAmount", 0) or 0) for c in wb["coin"])
        out["equity"] = out["usdt"] + out["btc_held"]*out["btc_price"] + out["usdc"]
    except Exception:
        out["btc_held"] = 0; out["usdt"] = 0; out["equity"] = 0; out["borrow"] = 0
    try:
        out["perp_short"] = 0
        for p in s.get_positions(category="linear", symbol="BTCUSDT")["result"]["list"]:
            if p.get("side") == "Sell": out["perp_short"] = float(p.get("size", 0) or 0)
    except Exception: out["perp_short"] = 0
    try:
        fr = [float(x["fundingRate"]) for x in s.get_funding_rate_history(category="linear", symbol="BTCUSDT", limit=9)["result"]["list"]]
        out["fund_day3"] = sum(fr[:9])/9*3*100 if fr else 0
    except Exception: out["fund_day3"] = 0
    meta = dict((r["k"], r["val"]) for r in q(TREND_DB, "select k,val from meta") if "err" not in r.keys())
    out["baseline"] = float(meta.get("initial_equity", 1002)); out["start_ts"] = meta.get("start_ts", "")
    try:
        out["op_days"] = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(out["start_ts"])).total_seconds()/86400 if out["start_ts"] else 1
    except: out["op_days"] = 1
    state = q(TREND_DB, "select position,entry_price from state where id=1")
    if state and "err" not in state[0].keys():
        out["trend_pos"] = state[0]["position"]; out["trend_entry"] = state[0]["entry_price"]
    else:
        out["trend_pos"] = "?"; out["trend_entry"] = None
    out["pnl_pct"] = (out["equity"]-out["baseline"])/out["baseline"]*100 if out["baseline"] > 0 else 0
    out["carry"] = q(CARRY_DB, "select ts,funding,short_btc,action from carry_log order by rowid desc limit 5")
    try:
        kl = s.get_kline(category="spot", symbol="BTCUSDT", interval="D", limit=200)["result"]["list"]
        cl = sorted(([int(x[0]), float(x[4])] for x in kl), key=lambda z: z[0])
        t0 = int(datetime.datetime.now(datetime.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
        closes = [x[1] for x in cl if x[0] < t0]
        out["sma125"] = sum(closes[-125:])/125
        out["sma200"] = sum(closes[-200:])/200 if len(closes) >= 200 else out["sma125"]
        out["exit_line"] = out["sma125"]*0.98
    except Exception:
        out["sma125"] = 0; out["sma200"] = 0; out["exit_line"] = 0
    out["variants"] = q(PAPER_DB, "select variant,equity,leverage,active,pos_entry_price,pos_entry_equity,peak_equity_since_entry,cooldown_until_date from pstate order by variant")
    out["v_stats"] = {}
    for vr in out["variants"]:
        if "err" in vr.keys(): continue
        v = vr["variant"]
        eqs = [r["equity"] for r in q(PAPER_DB, f"select equity from pequity where variant='{v}' order by ts") if "err" not in r.keys()]
        if not eqs: out["v_stats"][v] = (0,0,0,0); continue
        pk = eqs[0]; mdd = 0
        for x in eqs:
            pk = max(pk, x); mdd = min(mdd, (x-pk)/pk*100)
        last = q(PAPER_DB, f"select liq_dist from pequity where variant='{v}' order by ts desc limit 1")
        ld = last[0]["liq_dist"] if last and "err" not in last[0].keys() else 0
        out["v_stats"][v] = ((eqs[-1]/eqs[0]-1)*100, mdd, ld, len(eqs))
    bm = q(PAPER_DB, "select * from base_meta where id=1")
    out["grade"] = bm[0] if bm and "err" not in bm[0].keys() else None
    liq = q(PAPER_DB, "select count(*) c from pequity where event like '%LIQUID%'")
    tp = q(PAPER_DB, "select count(*) c from pequity where event like '%TP_HIT%'")
    viol = q(PAPER_DB, "select count(*) c from ai_log where violation=1")
    fail = q(PAPER_DB, "select count(*) c from ai_log where ok=0")
    out["liq_count"] = liq[0]["c"] if liq and "c" in liq[0].keys() else 0
    out["tp_count"] = tp[0]["c"] if tp and "c" in tp[0].keys() else 0
    out["viol"] = viol[0]["c"] if viol and "c" in viol[0].keys() else 0
    out["fail"] = fail[0]["c"] if fail and "c" in fail[0].keys() else 0
    out["ai_log"] = q(PAPER_DB, "select ts,variant,raw_leverage,effective_leverage,violation,reason from ai_log order by rowid desc limit 10")
    out["svc"] = {sv: is_active(sv) for sv in ("metis-v4-bot", "metis-v5-carry", "metis-v5-paper", "metis-v5-paper-xrp", "metis-v5-report.timer", "metis-v5-dashboard")}
    out["free_mem"] = free_mem(); out["disk_free"] = disk_free()
    out["usdkrw"] = get_usdkrw()
    # XRP paper
    xst = q(XRP_DB, "select * from xstate where id=1")
    out["xrp_state"] = xst[0] if xst and "err" not in xst[0].keys() else None
    xdaily = q(XRP_DB, "select * from xdaily order by date desc limit 1")
    out["xrp_daily"] = xdaily[0] if xdaily and "err" not in xdaily[0].keys() else None
    xeq_hist = q(XRP_DB, "select equity from xequity order by ts")
    xeqs = [r["equity"] for r in xeq_hist if "err" not in r.keys()]
    if xeqs:
        pk = xeqs[0]; mdd = 0
        for x in xeqs:
            pk = max(pk, x); mdd = min(mdd, (x-pk)/pk*100)
        out["xrp_stats"] = ((xeqs[-1]/xeqs[0]-1)*100, mdd, len(xeqs))
    else:
        out["xrp_stats"] = (0, 0, 0)
    xev = q(XRP_DB, "select ts,event from xequity where event not in ('init','no_pos') order by rowid desc limit 5")
    out["xrp_events"] = [r for r in xev if "err" not in r.keys()]
    # XRP perp 실시간 가격
    try:
        xt = s.get_tickers(category="linear", symbol="XRPUSDT")["result"]["list"][0]
        out["xrp_price"] = float(xt["lastPrice"])
        out["xrp_chg24"] = float(xt.get("price24hPcnt", 0))*100
    except Exception:
        out["xrp_price"] = 0; out["xrp_chg24"] = 0
    # XRP live bot
    xlst = q(XRP_LIVE_DB, "select * from xstate where id=1")
    out["xrp_live_state"] = xlst[0] if xlst and "err" not in xlst[0].keys() else None
    xllog = q(XRP_LIVE_DB, "select ts,event,detail from xlog order by rowid desc limit 6")
    out["xrp_live_log"] = [r for r in xllog if "err" not in r.keys()]
    # XRP live 실제 포지션 (Bybit)
    out["xrp_live_pos"] = None
    try:
        for p in s.get_positions(category="linear", symbol="XRPUSDT")["result"]["list"]:
            sz = float(p.get("size", 0) or 0)
            if sz > 0:
                out["xrp_live_pos"] = dict(side=p["side"], size=sz, entry=float(p["avgPrice"]),
                                            unrealized=float(p.get("unrealisedPnl", 0) or 0))
                break
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────
# RENDER — 라이트 테마. cream 배경 (#faf8f4) · 화이트 카드 · indigo primary · 큰 숫자
# ─────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: #faf8f4; color: #1a1d24; font-family: -apple-system, 'SF Pro Display', 'Inter', system-ui, sans-serif; font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1480px; margin: 0 auto; padding: 28px 32px 80px; }

.topbar { display: flex; align-items: center; justify-content: space-between; padding: 14px 22px; background: #ffffff; border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); margin-bottom: 24px; }
.brand { display: flex; align-items: center; gap: 14px; }
.brand-mark { width: 38px; height: 38px; border-radius: 11px; background: linear-gradient(135deg, #4f46e5, #6366f1); display: flex; align-items: center; justify-content: center; color: white; font-weight: 800; font-size: 15px; box-shadow: 0 6px 16px rgba(79,70,229,0.32); letter-spacing: -0.02em; }
.brand-title { font-size: 17px; font-weight: 700; letter-spacing: -0.015em; color: #0f172a; }
.brand-sub { font-size: 12px; color: #6b7280; font-weight: 500; margin-top: 1px; }
.topmeta { display: flex; gap: 18px; align-items: center; font-size: 12px; color: #4b5563; }
.topmeta b { color: #0f172a; font-weight: 600; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #10b981; margin-right: 5px; box-shadow: 0 0 0 3px rgba(16,185,129,0.20); vertical-align: middle; }

.hero { background: #ffffff; border-radius: 18px; padding: 30px 34px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); }
.hero-grid { display: grid; grid-template-columns: 1.5fr 1fr 1fr 1fr; gap: 36px; }
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

.live-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.live-card { background: #ffffff; border-radius: 14px; padding: 22px 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); border-top: 4px solid #4f46e5; position: relative; }
.live-card.carry { border-top-color: #0ea5e9; }
.live-card.xrp { border-top-color: #f59e0b; }
.live-h { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.live-name { font-size: 16px; font-weight: 700; color: #0f172a; letter-spacing: -0.01em; }
.live-cap { font-size: 11px; color: #6b7280; font-weight: 600; padding: 3px 9px; background: #f6f5f0; border-radius: 6px; }
.live-desc { font-size: 12px; color: #6b7280; margin-bottom: 14px; line-height: 1.55; }
.mini-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 18px; padding: 12px 0; border-top: 1px dashed #f0eee7; margin-top: 8px; }
.mini-grid div { font-size: 13px; }
.mlbl { display: block; font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 700; margin-bottom: 3px; }

.stat-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 18px; padding: 14px 0; border-top: 1px solid #f1f0eb; border-bottom: 1px solid #f1f0eb; margin: 8px 0 14px; }
.stat-l { font-size: 10px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 700; margin-bottom: 4px; }
.stat-v { font-size: 17px; font-weight: 700; color: #0f172a; font-variant-numeric: tabular-nums; }
.stat-sub { font-size: 11px; font-weight: 600; color: #6b7280; margin-left: 4px; }
.row-mini { display: flex; flex-wrap: wrap; gap: 10px 20px; padding: 12px 0; border-top: 1px dashed #f0eee7; font-size: 12.5px; color: #4b5563; }

.badge-on { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #d1fae5; color: #065f46; border-radius: 6px; letter-spacing: 0.01em; }
.badge-off { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #f3f4f6; color: #6b7280; border-radius: 6px; }
.badge-wait { display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 700; background: #fef3c7; color: #92400e; border-radius: 6px; }
.pos { color: #059669; font-weight: 700; }
.neg { color: #dc2626; font-weight: 700; }
.mut { color: #6b7280; font-weight: 600; }
.muted { color: #9ca3af; font-size: 12px; }

.svc-grid { display: flex; flex-wrap: wrap; gap: 10px; }
.svc-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; background: #faf8f4; border-radius: 10px; font-size: 12.5px; font-weight: 600; color: #1a1d24; box-shadow: 0 0 0 1px rgba(15,23,42,0.06); }
.svc-dot { width: 8px; height: 8px; border-radius: 50%; }
.svc-on { background: #10b981; box-shadow: 0 0 0 3px rgba(16,185,129,0.20); }
.svc-off { background: #d1d5db; }

.paper-group { background: #ffffff; border-radius: 14px; padding: 18px 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 0 0 1px rgba(15,23,42,0.05); margin-bottom: 14px; }
.pg-h { padding-left: 14px; display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; }
.pg-name { font-size: 14px; font-weight: 700; color: #0f172a; letter-spacing: -0.005em; }
.pg-count { font-size: 11px; color: #9ca3af; font-weight: 600; }
.vgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
.vcard { background: #faf8f4; border-radius: 10px; padding: 14px 16px; box-shadow: 0 0 0 1px rgba(15,23,42,0.04); }
.vcard-h { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.vname { font-size: 13px; font-weight: 700; color: #0f172a; font-family: 'SF Mono', 'Menlo', monospace; letter-spacing: -0.01em; }
.vstat { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; margin-bottom: 8px; }
.vstat-l { font-size: 10px; color: #9ca3af; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
.vstat-v { font-size: 14px; font-weight: 700; color: #0f172a; font-variant-numeric: tabular-nums; }
.liq-pill { display: inline-block; font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 6px; margin-top: 4px; }
.liq-pill.danger { background: #fee2e2; color: #b91c1c; }
.liq-pill.warn { background: #fef3c7; color: #92400e; }
.liq-pill.ok { background: #d1fae5; color: #065f46; }
.vdetails { margin-top: 10px; font-size: 11.5px; }
.vdetails summary { cursor: pointer; color: #6b7280; font-weight: 600; padding: 2px 0; }
.vdesc { margin-top: 6px; color: #4b5563; line-height: 1.5; padding: 6px 0; }

.grade-pill { display: inline-block; padding: 6px 14px; font-size: 12px; font-weight: 700; border-radius: 8px; }
.grade-pill.ok { background: #d1fae5; color: #065f46; }
.grade-pill.warn { background: #fef3c7; color: #92400e; }
.grade-pill.danger { background: #fee2e2; color: #b91c1c; }

.ev-h { font-size: 11px; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; margin: 14px 0 8px; }
.ev-row { padding: 6px 0; font-size: 12.5px; color: #4b5563; border-bottom: 1px dashed #f0eee7; }
.ev-row:last-child { border-bottom: 0; }
.ev-ts { display: inline-block; font-family: 'SF Mono', 'Menlo', monospace; font-size: 11px; color: #9ca3af; min-width: 100px; font-weight: 600; }
.ev-msg { color: #1a1d24; font-weight: 500; }

.ai-table { width: 100%; border-collapse: collapse; }
.ai-table th { font-size: 10.5px; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; text-align: left; padding: 10px 12px; border-bottom: 1px solid #f1f0eb; }
.ai-table td { padding: 11px 12px; font-size: 12.5px; color: #1a1d24; border-bottom: 1px solid #f6f5f0; }
.ai-table tr.ai-viol td { background: #fff7e6; }
.ai-ts { font-family: 'SF Mono', 'Menlo', monospace; color: #6b7280; font-size: 11.5px; }
.ai-reason { color: #6b7280; font-size: 11.5px; max-width: 480px; }

.sys-pill { display: inline-block; padding: 3px 9px; font-size: 11px; font-weight: 700; border-radius: 6px; }
.sys-pill.ok { background: #d1fae5; color: #065f46; }
.sys-pill.warn { background: #fef3c7; color: #92400e; }
.sys-pill.danger { background: #fee2e2; color: #b91c1c; }

footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #f0eee7; font-size: 11.5px; color: #9ca3af; text-align: center; }
"""


def _render_xrp_section(d):
    """XRP MR 페이퍼 카드 (라이트 테마)."""
    xst = d.get("xrp_state"); xdaily = d.get("xrp_daily")
    xret, xmdd, xpts = d.get("xrp_stats", (0,0,0))
    if not xst:
        return '<div class="card"><div class="card-h">XRP 박스 MR 페이퍼</div><div class="muted">데이터 없음 (서비스 시작 중)</div></div>'
    act_class = "badge-on" if xst["active"] else "badge-off"
    act_text = "활성" if xst["active"] else "현금"
    profit_usd = xst["equity"] - 1000.0
    p_cls = color(profit_usd)
    pos_block = ""
    if xst["active"]:
        upnl = ((xdaily["xrp_close"]/xst["entry_price"]-1)*100 * xst["leverage"]) if xst["entry_price"]>0 and xdaily else 0
        up_cls = color(upnl)
        pos_block = (
            '<div class="row-mini">'
            f'<div><span class="mlbl">진입</span> <b>{xst["leverage"]}x @ ${xst["entry_price"]:.4f}</b></div>'
            f'<div><span class="mlbl">종가</span> <b>${xdaily["xrp_close"]:.4f}</b> <span class="{up_cls}">({upnl:+.2f}%)</span></div>'
            f'<div><span class="mlbl">TP</span> <b>${xst["tp_price"]:.4f}</b></div>'
            f'<div><span class="mlbl">SL</span> <b>${xst["sl_price"]:.4f}</b></div>'
            f'<div><span class="mlbl">보유</span> <b>{xst["days_held"]}일</b></div>'
            '</div>'
        )
    kelt_block = ""
    if xdaily:
        try:
            btc_ok = "🟢 OK" if xdaily["btc_filter_ok"] else "🔴 RISK OFF"
            kelt_block = (
                '<div class="row-mini">'
                f'<div><span class="mlbl">Keltner (t-1)</span> <b>${xdaily["keltner_lower"]:.4f}</b> / ${xdaily["keltner_mid"]:.4f} / ${xdaily["keltner_upper"]:.4f}</div>'
                f'<div><span class="mlbl">폭</span> <b>{xdaily["width_pct"]*100:.1f}%</b></div>'
                f'<div><span class="mlbl">BTC 필터</span> <b>{btc_ok}</b></div>'
                f'<div><span class="mlbl">신호</span> <b>{xdaily["signal"]}</b></div>'
                '</div>'
            )
        except Exception:
            kelt_block = ""
    ev_lines = ""
    for e in d.get("xrp_events", []):
        ev_lines += f'<div class="ev-row"><span class="ev-ts">{e["ts"][11:19]}</span> <span class="ev-msg">{e["event"]}</span></div>'
    if not ev_lines:
        ev_lines = '<div class="muted">이벤트 없음 (대기 중)</div>'
    return (
        '<div class="card">'
        '<div class="card-h">XRP 박스 MR <span class="card-sub">— 페이퍼 검증 (별도, Stage 0)</span></div>'
        '<div class="desc">XRP perp Keltner_20_1.5 mean-reversion + BTC risk-on 필터 + 1.5x. 박스 하단에서 사고 평균선에서 팔기. 월 1번 정도 거래.</div>'
        '<div class="stat-row">'
        f'<div><div class="stat-l">상태</div><div class="stat-v"><span class="{act_class}">{act_text}</span></div></div>'
        f'<div><div class="stat-l">자산</div><div class="stat-v">${xst["equity"]:.2f}</div></div>'
        f'<div><div class="stat-l">수익</div><div class="stat-v {p_cls}">${profit_usd:+.2f}<span class="stat-sub">({xret:+.2f}%)</span></div></div>'
        f'<div><div class="stat-l">MDD</div><div class="stat-v neg">{xmdd:.1f}%</div></div>'
        f'<div><div class="stat-l">포인트</div><div class="stat-v">{xpts}</div></div>'
        '</div>'
        f'{pos_block}'
        f'{kelt_block}'
        '<div class="ev-h">최근 이벤트</div>'
        f'{ev_lines}'
        '</div>'
    )


def render(d):
    """모든 카드/섹션을 한 HTML 문자열로 조립."""
    rate = d.get("usdkrw", 1370)
    eq = d.get("equity", 0); base = d.get("baseline", 1002)
    pnl_usd = eq - base; pnl_pct = d.get("pnl_pct", 0)
    pnl_cls = color(pnl_usd)
    pnl_arrow = "▲" if pnl_usd > 0 else ("▼" if pnl_usd < 0 else "—")
    btc_p = d.get("btc_price", 0); btc_chg = d.get("btc_chg24", 0)
    btc_cls = color(btc_chg)
    xrp_p = d.get("xrp_price", 0); xrp_chg = d.get("xrp_chg24", 0)
    xrp_cls = color(xrp_chg)
    sma125 = d.get("sma125", 0)
    above_sma = (btc_p - sma125) / sma125 * 100 if sma125 > 0 else 0
    trend_label = "추세 ON" if above_sma > 0 else "추세 OFF"
    trend_badge_cls = "badge-on" if above_sma > 0 else "badge-off"

    # ── LIVE 1: 추세봇 ──
    trend_pos = d.get("trend_pos", "?")
    trend_entry = d.get("trend_entry")
    trend_held = d.get("btc_held", 0)
    trend_val = trend_held * btc_p
    if trend_pos == "LONG" and trend_entry:
        trend_upnl_pct = (btc_p / trend_entry - 1) * 100
        trend_upnl_usd = trend_held * (btc_p - trend_entry)
        trend_cls = color(trend_upnl_pct)
        trend_info = (
            '<div class="mini-grid">'
            f'<div><span class="mlbl">진입가</span> <b>${trend_entry:,.0f}</b></div>'
            f'<div><span class="mlbl">미실현</span> <b class="{trend_cls}">{trend_upnl_pct:+.2f}%</b> <span class="{trend_cls}">(${trend_upnl_usd:+.2f})</span></div>'
            '</div>'
        )
    else:
        trend_info = '<div class="muted">포지션 없음 / 현금 대기</div>'

    # ── LIVE 2: Carry ──
    carry_rows = d.get("carry", [])
    fund_day3 = d.get("fund_day3", 0)
    fund_cls = color(fund_day3)
    perp_short = d.get("perp_short", 0)
    carry_active = perp_short > 0
    carry_badge = '<span class="badge-on">헷지 중</span>' if carry_active else '<span class="badge-off">대기</span>'
    carry_recent = ""
    for r in carry_rows[:3]:
        if "err" in r.keys(): continue
        try:
            fr3 = float(r["funding"]) * 100 * 3
            carry_recent += f'<div class="ev-row"><span class="ev-ts">{r["ts"][5:16]}</span> <span class="ev-msg">{r["action"]} · 3일 funding {fr3:+.3f}%</span></div>'
        except Exception:
            pass
    if not carry_recent:
        carry_recent = '<div class="muted">로그 없음</div>'

    # ── LIVE 3: XRP 라이브 ──
    xls = d.get("xrp_live_state")
    xlp = d.get("xrp_live_pos")
    if xlp:
        xl_upnl = xlp["unrealized"]
        xl_upnl_pct = (xrp_p / xlp["entry"] - 1) * 100 * 1.5 if xlp["entry"] > 0 else 0
        xl_cls = color(xl_upnl)
        xl_status = f'<span class="badge-on">{xlp["side"]} 진행 중</span>'
        xl_pos_block = (
            '<div class="mini-grid">'
            f'<div><span class="mlbl">진입가</span> <b>${xlp["entry"]:.4f}</b></div>'
            f'<div><span class="mlbl">크기</span> <b>{xlp["size"]:.0f} XRP</b></div>'
            f'<div><span class="mlbl">미실현 PnL</span> <b class="{xl_cls}">${xl_upnl:+.2f}</b> <span class="{xl_cls}">({xl_upnl_pct:+.2f}%)</span></div>'
            '</div>'
        )
    else:
        if xls and ("active" in xls.keys()) and xls["active"]:
            xl_status = '<span class="badge-on">활성</span>'
        else:
            xl_status = '<span class="badge-wait">진입 대기</span>'
        xl_pos_block = '<div class="muted">포지션 없음 — 매일 09:05 KST Keltner 하단 도달 시 진입</div>'
    xl_log = ""
    for e in d.get("xrp_live_log", [])[:3]:
        xl_log += f'<div class="ev-row"><span class="ev-ts">{e["ts"][5:16]}</span> <span class="ev-msg">{e["event"]}</span></div>'
    if not xl_log:
        xl_log = '<div class="muted">로그 없음</div>'

    # ── 서비스 ──
    SVC_LABELS = {
        "metis-v4-bot": "추세봇",
        "metis-v5-carry": "Carry",
        "metis-v5-paper": "페이퍼 (BTC)",
        "metis-v5-paper-xrp": "페이퍼 (XRP)",
        "metis-v5-dashboard": "대시보드",
        "metis-v5-report.timer": "리포트 타이머",
    }
    svc_html = ""
    for sv, on in d.get("svc", {}).items():
        lbl = SVC_LABELS.get(sv, sv)
        dot = "svc-on" if on else "svc-off"
        svc_html += f'<div class="svc-pill"><span class="svc-dot {dot}"></span>{lbl}</div>'

    # ── 페이퍼 변형 ──
    GROUPS = [
        ("기준선",     ["P0_1x", "P1_1.5x", "S1_buyhold"],                        "#0ea5e9"),
        ("Vol-Target", ["V_vt2", "V_vt5", "V_vt10"],                              "#8b5cf6"),
        ("AI 강제룰",   ["V_ai_pro", "V_ai_flash", "V_ai_full"],                  "#10b981"),
        ("AI 자율",    ["V_ai_pro_free", "V_ai_flash_free"],                      "#f59e0b"),
        ("AI 공격",    ["V_ai_pro_aggro", "V_ai_flash_aggro"],                    "#ef4444"),
        ("AI 공격+게이트", ["V_ai_pro_aggro_safe", "V_ai_flash_aggro_safe"],      "#ec4899"),
        ("Scalper",   ["V_tp10"],                                                 "#a855f7"),
    ]
    var_map = {}
    for vr in d.get("variants", []):
        if "err" in vr.keys(): continue
        var_map[vr["variant"]] = vr
    paper_html = ""
    for gname, gnames, gcolor in GROUPS:
        cards = ""
        for v in gnames:
            vr = var_map.get(v)
            if not vr: continue
            ret, mdd, ld, n = d["v_stats"].get(v, (0, 0, 0, 0))
            ret_cls = color(ret)
            active_badge = '<span class="badge-on">활성</span>' if vr["active"] else '<span class="badge-off">현금</span>'
            ld_html = ""
            if vr["active"] and ld > 0:
                ld_cls = "danger" if ld < 15 else ("warn" if ld < 30 else "ok")
                ld_html = f'<span class="liq-pill {ld_cls}">청산까지 {ld:.0f}%</span>'
            desc_short, desc_easy = VARIANT_DESC.get(v, ("", ""))
            cards += (
                '<div class="vcard">'
                '<div class="vcard-h">'
                f'<div class="vname">{v}</div>'
                f'<div>{active_badge}</div>'
                '</div>'
                '<div class="vstat">'
                f'<div><div class="vstat-l">수익</div><div class="vstat-v {ret_cls}">{ret:+.2f}%</div></div>'
                f'<div><div class="vstat-l">MDD</div><div class="vstat-v neg">{mdd:.1f}%</div></div>'
                f'<div><div class="vstat-l">레버리지</div><div class="vstat-v">{vr["leverage"]:.1f}x</div></div>'
                f'<div><div class="vstat-l">포인트</div><div class="vstat-v">{n}</div></div>'
                '</div>'
                f'{ld_html}'
                f'<details class="vdetails"><summary>설명</summary><div class="vdesc">{desc_easy}</div></details>'
                '</div>'
            )
        if cards:
            paper_html += (
                '<div class="paper-group">'
                f'<div class="pg-h" style="border-left:4px solid {gcolor}"><span class="pg-name">{gname}</span><span class="pg-count">{len(gnames)}개</span></div>'
                f'<div class="vgrid">{cards}</div>'
                '</div>'
            )

    # ── AI 로그 ──
    ai_rows = ""
    for r in d.get("ai_log", [])[:8]:
        if "err" in r.keys(): continue
        v_cls = "ai-viol" if r["violation"] else ""
        viol_mark = "⚠️ 위반" if r["violation"] else "✅"
        reason = (r["reason"] or "")[:90]
        ai_rows += f'<tr class="{v_cls}"><td class="ai-ts">{r["ts"][5:16]}</td><td><b>{r["variant"]}</b></td><td>{r["raw_leverage"]}x → <b>{r["effective_leverage"]}x</b></td><td>{viol_mark}</td><td class="ai-reason">{reason}</td></tr>'
    if not ai_rows:
        ai_rows = '<tr><td colspan="5" class="muted">AI 로그 없음</td></tr>'

    # ── Grade ──
    grade = d.get("grade")
    grade_html = ""
    if grade:
        try:
            tg = grade["trend_grade"] if "trend_grade" in grade.keys() else "?"
            tg_cls = "ok" if "positive" in str(tg) else ("warn" if str(tg) == "neutral" else "danger")
            grade_html = (
                '<div class="card" style="margin-bottom:14px">'
                f'<span class="grade-pill {tg_cls}">시장 등급: {tg}</span> '
                f'<span class="muted" style="margin-left:14px">청산 {d["liq_count"]} · TP {d["tp_count"]} · AI 위반 {d["viol"]} · 호출실패 {d["fail"]}</span>'
                '</div>'
            )
        except Exception:
            pass

    # ── 시스템 ──
    mem = d.get("free_mem", -1)
    mem_cls = "ok" if mem > 200 else ("warn" if mem > 100 else "danger")
    op_days = d.get("op_days", 1)
    disk = d.get("disk_free", "?")
    now_kst = d.get("now_kst", "")

    # ── 조립 ──
    html = []
    html.append('<!doctype html><html lang="ko"><head>')
    html.append('<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    html.append('<title>운영 대시보드</title>')
    html.append(f'<style>{CSS}</style></head><body><div class="wrap">')

    # TOPBAR
    html.append(
        '<div class="topbar">'
        '<div class="brand">'
        '<div class="brand-mark">M</div>'
        '<div>'
        '<div class="brand-title">운영 대시보드</div>'
        '<div class="brand-sub">실시간 통합 모니터 · F5로 갱신</div>'
        '</div>'
        '</div>'
        '<div class="topmeta">'
        f'<span><span class="dot"></span>운영 {op_days:.1f}일</span>'
        f'<span><b>{now_kst}</b> KST</span>'
        f'<span>USD/KRW <b>{rate:,.0f}</b></span>'
        f'<span>MEM <span class="sys-pill {mem_cls}">{mem}Mi</span></span>'
        f'<span>DISK <b>{disk}</b></span>'
        '</div>'
        '</div>'
    )

    # HERO
    html.append('<div class="hero"><div class="hero-grid">')
    html.append(
        '<div class="hero-cell">'
        '<div class="hero-l">총 자산</div>'
        f'<div class="hero-v">${eq:,.2f}</div>'
        f'<div class="hero-sub"><span class="{pnl_cls}">{pnl_arrow} ${abs(pnl_usd):,.2f} ({pnl_pct:+.2f}%)</span> · 원금 ${base:,.0f}</div>'
        f'<div class="hero-krw">{krw_str(eq, rate)} · 손익 {krw_str(pnl_usd, rate)}</div>'
        '</div>'
    )
    html.append(
        '<div class="hero-cell">'
        '<div class="hero-l">XRP 라이브 ($300)</div>'
        f'<div class="hero-v small">{xl_status}</div>'
        '<div class="hero-sub">슬리브 $300 · Keltner_20_1.5 · 1.5x</div>'
        f'<div class="hero-krw">XRP ${xrp_p:.4f} <span class="{xrp_cls}">({xrp_chg:+.2f}%)</span></div>'
        '</div>'
    )
    html.append(
        '<div class="hero-cell">'
        f'<div class="hero-l">BTC ({trend_label})</div>'
        f'<div class="hero-v">${btc_p:,.0f}</div>'
        f'<div class="hero-sub"><span class="{btc_cls}">{btc_chg:+.2f}% (24h)</span> · SMA125 ${sma125:,.0f}</div>'
        f'<div class="hero-krw"><span class="{trend_badge_cls}">{trend_label}</span> · SMA 대비 {above_sma:+.2f}%</div>'
        '</div>'
    )
    html.append(
        '<div class="hero-cell">'
        '<div class="hero-l">Funding (BTC 3일)</div>'
        f'<div class="hero-v {fund_cls}">{fund_day3:+.2f}%</div>'
        '<div class="hero-sub">Carry 신호 · 양수면 헷지 가능</div>'
        f'<div class="hero-krw">숏 {perp_short:.4f} BTC · {carry_badge}</div>'
        '</div>'
    )
    html.append('</div></div>')

    # LIVE TRADING
    html.append('<div class="sec-h"><h2>실전 운영 (3트랙)</h2><span class="sec-tag">live · 실제 돈</span></div>')
    html.append('<div class="live-grid">')
    html.append(
        '<div class="live-card">'
        f'<div class="live-h"><div class="live-name">① BTC 추세봇</div><div class="live-cap">시드 ~${trend_val:.0f}</div></div>'
        '<div class="live-desc">BTC 현물 1x. close &gt; SMA125 진입, close &lt; SMA125×0.98 청산. 펀딩비 없음.</div>'
        '<div class="mini-grid">'
        f'<div><span class="mlbl">상태</span> <b>{trend_pos}</b></div>'
        f'<div><span class="mlbl">보유</span> <b>{trend_held:.6f} BTC</b></div>'
        '</div>'
        f'{trend_info}'
        '</div>'
    )
    html.append(
        '<div class="live-card carry">'
        '<div class="live-h"><div class="live-name">② BTC Carry</div><div class="live-cap">델타 중립 · 무차입</div></div>'
        '<div class="live-desc">BTC 현물 + perp 숏 헷지. funding &gt; 0 시 자동 진입. 빚 없음.</div>'
        '<div class="mini-grid">'
        f'<div><span class="mlbl">상태</span> {carry_badge}</div>'
        f'<div><span class="mlbl">숏 사이즈</span> <b>{perp_short:.4f} BTC</b></div>'
        '</div>'
        '<div class="ev-h">최근 신호</div>'
        f'{carry_recent}'
        '</div>'
    )
    html.append(
        '<div class="live-card xrp">'
        '<div class="live-h"><div class="live-name">③ XRP 라이브 MR</div><div class="live-cap">$300 슬리브 · 1.5x</div></div>'
        '<div class="live-desc">XRP perp Keltner_20_1.5 mean-reversion. 박스 하단 진입, 평균선 익절. BTC 필터 ON.</div>'
        '<div class="mini-grid">'
        f'<div><span class="mlbl">상태</span> {xl_status}</div>'
        f'<div><span class="mlbl">XRP 가격</span> <b>${xrp_p:.4f}</b> <span class="{xrp_cls}">({xrp_chg:+.2f}%)</span></div>'
        '</div>'
        f'{xl_pos_block}'
        '<div class="ev-h">최근 로그</div>'
        f'{xl_log}'
        '</div>'
    )
    html.append('</div>')

    # SERVICES
    html.append('<div class="sec-h"><h2>시스템 서비스</h2><span class="sec-tag">systemd</span></div>')
    html.append(f'<div class="card"><div class="svc-grid">{svc_html}</div></div>')

    # PAPER
    html.append('<div class="sec-h"><h2>페이퍼 변형</h2><span class="sec-tag">검증 · 모의</span></div>')
    html.append(grade_html)
    html.append(paper_html)

    # AI LOG
    html.append('<div class="sec-h"><h2>AI 호출 로그</h2><span class="sec-tag">최근 8건</span></div>')
    html.append(
        '<div class="card"><table class="ai-table">'
        '<thead><tr><th>시간</th><th>변형</th><th>레버리지</th><th>검증</th><th>사유</th></tr></thead>'
        f'<tbody>{ai_rows}</tbody>'
        '</table></div>'
    )

    # XRP MR
    html.append('<div class="sec-h"><h2>XRP MR 페이퍼 (별도 검증)</h2><span class="sec-tag">stage 0</span></div>')
    html.append(_render_xrp_section(d))

    html.append('<footer>운영 대시보드 · 의존성 0 · bare http.server · ~20MB RAM · 자동 갱신 X (F5)</footer>')
    html.append('</div></body></html>')
    return "".join(html)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        try:
            d = gather(); html = render(d)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(f"Error: {e}".encode())
    def log_message(self, *a): pass


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8501"))
    print(f"dashboard listening on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
