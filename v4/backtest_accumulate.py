#!/usr/bin/env python3
"""
분할매수/물타기/리밸런싱 종합 백테스트.

분할매수·물타기·그리드·평균회귀·리밸런싱 계열 중 buy-and-hold(BH)를 일관되게 이기는
전략이 하나라도 있는지 확인하는 배터리. 스캘핑은 다루지 않는다. 일봉 기준, 4코인
(BTC/ETH/SOL/XRP), 현실적인 수수료 적용.

전략 목록:
  BH              매수 후 보유 (벤치마크)
  DCA_deploy      시작 자본을 N구간에 나눠 분할 진입 (거치식 분할, 외부 현금 흐름 없음)
  dip_avg         진입 후 가격이 trig% 내릴 때마다 15% 추가(물타기), 평단×(1+target) 회복 시 전량 매도→반복
  dip_avg_trend   dip_avg + 추세필터(SMA 위에서만 물타기, 폭락 칼날 회피)
  martingale      물타기인데 더 깊이 빠질수록 더 크게 (위험 비교용)
  grid            고정 그리드 (레인지 하단 매수 / 상단 매도)
  rsi_mr          RSI<30 분할매수 / RSI>70 분할매도
  bb_mr           볼린저 하단 매수 / 중심선 매도
  rebalance       목표비중 유지 리밸런싱 (자동 저가매수·고가매도 = 변동성 수확)
"""
import requests, time, datetime, math, os
import numpy as np

FEE = 0.001            # 현실 spot taker 0.1% (보수적). 비교 시엔 0.055%로 재실행하면 됨.
CASH0 = 10000.0
CACHE = "/home/botuser/metis-v4/btcache"


def fetch(sym, interval, days):
    """Bybit kline 다운로드 + 1일 디스크 캐싱."""
    os.makedirs(CACHE, exist_ok=True)
    fn = f"{CACHE}/{sym}_{interval}_{days}.npy"
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 86400:
        return np.load(fn)
    end = int(time.time() * 1000)
    start = end - days * 86400000
    allk = {}
    cur = end
    for _ in range(600):
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params={"category": "spot", "symbol": sym, "interval": interval, "end": cur, "limit": 1000}, timeout=20)
        lst = r.json().get("result", {}).get("list", [])
        if not lst:
            break
        for k in lst:
            allk[int(k[0])] = float(k[4])
        oldest = min(int(k[0]) for k in lst)
        if oldest <= start or len(lst) < 1000:
            break
        cur = oldest - 1
        time.sleep(0.06)
    arr = np.array([allk[t] for t in sorted(allk) if t >= start])
    np.save(fn, arr)
    return arr


def stats(eqc):
    """누적 수익률 / 최대 낙폭 / Sharpe(일봉 근사) 반환."""
    eqc = np.array(eqc, float)
    if len(eqc) < 2 or eqc[-1] <= 0:
        return -1, -1, 0
    ret = eqc[-1] / eqc[0] - 1
    peak = np.maximum.accumulate(eqc)
    mdd = ((eqc - peak) / peak).min()
    dr = np.diff(eqc) / eqc[:-1]
    sh = (np.nanmean(dr) / np.nanstd(dr) * math.sqrt(365)) if np.nanstd(dr) > 0 else 0
    return ret, mdd, sh


# ---------- 지표 ----------
def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    ru = np.zeros_like(c); rd = np.zeros_like(c)
    ru[n] = up[1:n+1].mean(); rd[n] = dn[1:n+1].mean()
    for i in range(n+1, len(c)):
        ru[i] = (ru[i-1]*(n-1)+up[i])/n; rd[i] = (rd[i-1]*(n-1)+dn[i])/n
    rs = np.divide(ru, rd, out=np.ones_like(c), where=rd != 0)
    return 100 - 100/(1+rs)


def sma(c, n):
    s = np.full(len(c), np.nan)
    for i in range(n-1, len(c)):
        s[i] = c[i-n+1:i+1].mean()
    return s


# ---------- 전략들 (단일 코인, 현금풀 모델) ----------
def bh(c):
    """매수 후 보유 — 첫날 전액 매수."""
    return list(CASH0 * c / c[0]), 1, CASH0*FEE


def dca_deploy(c, n_steps=20):
    """전체 자본을 N구간에 나눠 시간 분할 진입(거치식 DCA). 외부 현금 추가는 없음."""
    cash = CASH0; coins = 0.0; eqc = []; tr = 0; fees = 0
    step_amt = CASH0 / n_steps
    every = max(1, len(c)//n_steps)
    for i, p in enumerate(c):
        if i % every == 0 and cash >= step_amt - 1e-9 and tr < n_steps:
            coins += step_amt/p*(1-FEE); cash -= step_amt; fees += step_amt*FEE; tr += 1
        eqc.append(cash+coins*p)
    return eqc, tr, fees


def dip_avg(c, trig=0.05, frac=0.15, target=0.05, trend=None, martingale=False):
    """물타기: 마지막 매수가 대비 trig% 하락마다 추가 매수, 평단×(1+target) 도달 시 전량 매도→반복.
    trend=SMA 시리즈를 주면 추세 위에서만 진입(추세필터). martingale=True면 회차마다 크기 증가."""
    cash = CASH0; coins = 0.0; invested = 0.0; last = None; eqc = []; tr = 0; fees = 0; n_add = 0
    for i, p in enumerate(c):
        ok_trend = True if trend is None else (not np.isnan(trend[i]) and p > trend[i])
        if coins == 0 and cash > 0 and ok_trend:
            amt = CASH0*frac
            coins += amt/p*(1-FEE); cash -= amt; invested += amt; fees += amt*FEE; last = p; tr += 1; n_add = 1
        elif coins > 0 and last and p <= last*(1-trig) and ok_trend:
            mult = n_add if martingale else 1
            amt = min(CASH0*frac*mult, cash)
            if amt > 5:
                coins += amt/p*(1-FEE); cash -= amt; invested += amt; fees += amt*FEE; last = p; tr += 1; n_add += 1
        if coins > 0:
            avg = invested/coins
            if p >= avg*(1+target):
                cash += coins*p*(1-FEE); fees += coins*p*FEE; coins = 0; invested = 0; last = None; tr += 1; n_add = 0
        eqc.append(cash+coins*p)
    return eqc, tr, fees


def grid(c, levels=10, band=0.4, frac=0.1):
    """고정 그리드 — 시작가 ±band 범위에 levels개 라인. 단순 매수 트랜치만 (매도 로직 단순화)."""
    lo, hi = c[0]*(1-band), c[0]*(1+band)
    grid_p = np.linspace(lo, hi, levels)
    cash = CASH0; coins = 0.0; eqc = []; tr = 0; fees = 0
    bought = set()
    for p in c:
        for j, g in enumerate(grid_p):
            if p <= g and j not in bought and cash >= CASH0*frac:
                amt = CASH0*frac; coins += amt/p*(1-FEE); cash -= amt; fees += amt*FEE; bought.add(j); tr += 1
            elif p >= g and j in bought:
                sell = coins*0.0
        # 매도 로직은 단순화: 평단×(1+target) 도달 시만 전량 매도 (별도 함수 호출 없이 인라인 처리는 생략)
        eqc.append(cash+coins*p)
    return eqc, tr, fees


def rsi_mr(c, buy=30, sell=70, frac=0.15, target=0.05):
    """RSI 평균회귀 — RSI<buy면 분할매수, RSI>sell 또는 평단+target 도달 시 전량 매도."""
    r = rsi(c); cash = CASH0; coins = 0.0; invested = 0.0; eqc = []; tr = 0; fees = 0
    for i, p in enumerate(c):
        if r[i] < buy and cash >= CASH0*frac:
            amt = CASH0*frac; coins += amt/p*(1-FEE); cash -= amt; invested += amt; fees += amt*FEE; tr += 1
        if coins > 0:
            avg = invested/coins
            if r[i] > sell or p >= avg*(1+target):
                cash += coins*p*(1-FEE); fees += coins*p*FEE; coins = 0; invested = 0; tr += 1
        eqc.append(cash+coins*p)
    return eqc, tr, fees


def bb_mr(c, n=20, k=2.0, frac=0.15):
    """볼린저 평균회귀 — 하단 이탈 매수, 중심선 도달 매도."""
    cash = CASH0; coins = 0.0; invested = 0.0; eqc = []; tr = 0; fees = 0
    for i, p in enumerate(c):
        if i >= n:
            w = c[i-n:i]; m = w.mean(); sd = w.std()
            if p < m-k*sd and cash >= CASH0*frac:
                amt = CASH0*frac; coins += amt/p*(1-FEE); cash -= amt; invested += amt; fees += amt*FEE; tr += 1
            elif coins > 0 and p > m:
                cash += coins*p*(1-FEE); fees += coins*p*FEE; coins = 0; invested = 0; tr += 1
        eqc.append(cash+coins*p)
    return eqc, tr, fees


def rebalance(c, target=0.6, band=0.1):
    """목표비중 리밸런싱 — 비중이 band를 벗어나면 target으로 재조정(변동성 수확)."""
    coins = (CASH0*target)/c[0]*(1-FEE); cash = CASH0*(1-target); eqc = []; tr = 1; fees = CASH0*target*FEE
    for p in c:
        val = coins*p; tot = cash+val; frac = val/tot if tot > 0 else 0
        if frac > target+band:
            sell_val = (frac-target)*tot; coins -= sell_val/p; cash += sell_val*(1-FEE); fees += sell_val*FEE; tr += 1
        elif frac < target-band and cash > 0:
            buy_val = min((target-frac)*tot, cash); coins += buy_val/p*(1-FEE); cash -= buy_val; fees += buy_val*FEE; tr += 1
        eqc.append(cash+coins*p)
    return eqc, tr, fees


COINS = {"BTCUSDT": 1500, "ETHUSDT": 1500, "SOLUSDT": 1500, "XRPUSDT": 1500}

print("="*100)
print("분할매수/물타기/리밸런싱 종합 — 일봉. 수수료 0.1%/체결. BH 대비.")
print("="*100)

for sym, days in COINS.items():
    c = fetch(sym, "D", days)
    if len(c) < 200:
        print(f"\n{sym}: 데이터 부족({len(c)})"); continue
    trend = sma(c, 125)
    bh_eqc, _, _ = bh(c)
    bh_ret = bh_eqc[-1]/bh_eqc[0]-1
    print(f"\n[{sym}] {len(c)}일")
    strats = [
        ("BH(보유)", bh(c)),
        ("DCA 분할진입", dca_deploy(c)),
        ("물타기 5%/+5%", dip_avg(c, 0.05, 0.15, 0.05)),
        ("물타기 8%/+8%", dip_avg(c, 0.08, 0.15, 0.08)),
        ("물타기 10%/+10%", dip_avg(c, 0.10, 0.15, 0.10)),
        ("물타기+추세필터", dip_avg(c, 0.05, 0.15, 0.05, trend=trend)),
        ("마틴게일", dip_avg(c, 0.08, 0.10, 0.08, martingale=True)),
        ("RSI 평균회귀", rsi_mr(c)),
        ("볼린저 평균회귀", bb_mr(c)),
        ("리밸런싱 60/40", rebalance(c, 0.6, 0.1)),
        ("리밸런싱 50/50", rebalance(c, 0.5, 0.1)),
    ]
    for name, (eqc, tr, fees) in strats:
        ret, mdd, sh = stats(eqc)
        win = "✅" if ret > bh_ret else "  "
        print(f"  {win} {name:<16} ret {ret*100:+8.0f}%  MDD {mdd*100:6.0f}%  Sharpe {sh:5.2f}  거래 {tr:>4}  수수료 ${fees:>6.0f}")
    print(f"     (BH ret {bh_ret*100:+.0f}%)")

print("\n해석: ✅ = BH 초과. 4종목에 걸쳐 일관되게 ✅ 나는 전략이 없으면 분할매수 계열은 엣지 없음.")
