#!/usr/bin/env python3
"""SMA125 추세 룰의 거래 단위 통계 — Binance 일봉으로 2017년부터 백테스트.

각 거래(진입→청산)별로 수익률/보유일을 모아 평균/중앙값/승률/분포를 출력.
거래 수가 적은(연 ~1-2회) 추세 전략의 통계적 특성을 직관적으로 보기 위함.
"""
import requests, time, datetime
import numpy as np

FEE = 0.00055                  # 편도 수수료 (Binance VIP 가정)


def fetch(sym):
    """Binance 일봉 전체 다운로드 (1000건 단위 페이지네이션)."""
    start = int(datetime.datetime(2017, 1, 1, tzinfo=datetime.timezone.utc).timestamp()*1000)
    out = {}; cur = start
    for _ in range(80):
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": sym, "interval": "1d", "startTime": cur, "limit": 1000},
                         timeout=20).json()
        if not isinstance(r, list) or not r: break
        for k in r: out[int(k[0])] = k
        last = int(r[-1][0])
        if last <= cur or len(r) < 1000: break
        cur = last + 86400000; time.sleep(0.05)
    return [out[t] for t in sorted(out)]


ks = fetch("BTCUSDT")
c = np.array([float(k[4]) for k in ks])
N = 125; buf = 0.02; st = 130       # SMA 기간 / 청산 버퍼 / 통계 시작 인덱스

# SMA125 사전 계산
s = np.full(len(c), np.nan)
for i in range(N-1, len(c)):
    s[i] = c[i-N+1:i+1].mean()

# 시뮬레이션: 단일 포지션 진입/청산 반복
pos = 0; ent = 0; ei = 0; tr = []
for i in range(st, len(c)):
    if pos == 0 and c[i] > s[i]:
        pos = 1; ent = c[i]; ei = i
    elif pos == 1 and c[i] < s[i]*(1-buf):
        tr.append(((c[i]/ent-1) - 2*FEE, i-ei)); pos = 0
if pos == 1:
    tr.append(((c[-1]/ent-1) - 2*FEE, len(c)-1-ei, "open"))

# 통계 출력
rets = [t[0]*100 for t in tr]
wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
print(f"총 거래 {len(tr)}개 ({len(ks)}일 / {len(ks)/365:.1f}년) = 연 {len(tr)/(len(ks)/365):.1f}회")
print(f"승률 {len(wins)/len(tr)*100:.0f}% ({len(wins)}승 {len(losses)}패)")
print(f"평균 수익(승): {np.mean(wins):+.0f}%  | 평균 손실(패): {np.mean(losses):+.1f}%")
print(f"전체 평균/거래: {np.mean(rets):+.0f}%  | 중앙값: {np.median(rets):+.1f}%")
print(f"최대 winner: {max(rets):+.0f}%  | 최대 loser: {min(rets):+.1f}%")
print(f"승 분포: " + ", ".join(f"{r:+.0f}%" for r in sorted(wins, reverse=True)))
print(f"평균 보유: 승 {np.mean([tr[i][1] for i in range(len(tr)) if rets[i]>0]):.0f}일 / 패 {np.mean([tr[i][1] for i in range(len(tr)) if rets[i]<=0]):.0f}일")
