# METIS v2 (METIS-F2 검증 v2)

암호화폐 무기한 선물 자동매매 시스템 — 2세대.

Bybit BTCUSDT + XRPUSDT 멀티 심볼. AI Full Delegation (방향/레버리지/SL/TP 모두 AI).


---

## 개요

METIS v2는 2026년 5월 초~5월 12일 운영. ATHENA(spot 봇) 종료 후 *perpetual futures 단독 운영*으로 전환한 첫 버전.

설계 철학:
- AI = 판단자 (단순 필터 → full delegation 전환)
- 코드 = 데이터 전달자 + 안전 검증만
- 2-prompt-bidirectional (LONG 분석 + SHORT 분석 별도 호출)
- AI가 next_recheck_hours 직접 결정 (1-24h)

---

## v1 대비 주요 변경

- 멀티 심볼 (BTC + XRP)
- AI Full Delegation (direction + leverage + SL/TP 모두 AI 판단)
- 중간 점검(recheck) 시스템 추가 (HOLD/MODIFY/EXIT)
- Profit Guard v2 도입 (3-trigger: Multi-TF reversal + peak drawdown + hard drawdown)
- Cycle log JSON 저장 (시장 데이터 + AI 결정 + 결과 풀 컨텍스트)
- Paper trading mode 추가
- ATR 기반 SL/TP 거리 (Fix #18~#25 누적)

---

## Fix 이력 (주요)

- Fix #18 (5/9): LONG/SHORT analysis 가이드 (SL ATR×2 + R:R + Leverage)
- Fix #19 (5/9): main.py recheck flow (멀티심볼 누수 fix)
- Fix #20 (5/10): EXIT-3B "수익권 + 추세 소진 시 cleanly EXIT" 가이드
- Fix #21 (5/11): 첫 recheck 시간 AI 권장값 (hardcoded 4h 제거)
- Fix #22b/c (5/11): SL 가이드 "ATR×2가 안전선" 강화
- Fix #23 (5/12): 체결 조회 대기 5초 → 19초 (closing fee 누락 fix)
- Fix #24 (5/12): 진입 자리 가이드 (24h 박스 위치 인식)
- Fix #25 (5/12 직전): 부분 보강

---

## 결과

9 거래 청산 + 노이즈 1 + 진행 1:
- net **−$3.47** (Bybit 실측)
- 승률 3승 4패 (43%) + 노이즈 1
- 손실 패턴: 박스 상단 LONG + 반전 신호 X (과매수권 추격)
- ATR<1.5 진입 = 100% 손실 / ATR≥2 = 75% 흑자 발견

---

## 폐기 사유

2026-05-12 운영자 결정으로 *완전 새시작* (검증 v3 = METIS v3 시작):

- 9 거래 표본 — 통계 가치 한정
- Fix #18-25 누적 패치 — 사이드 효과 추적 어려움
- 시드 추가 직전 깨끗한 출발점 확보
- DB clear + 새 prompt 재작성

v2 코드는 *역사 기록*. 운영 X. 데이터는 `trades_v2.csv`로 보존.

---

## 프로젝트 구조

```
v2/
├── main.py
├── ai/
│   ├── prompts.py            # LONG/SHORT/recheck 프롬프트 (Fix #25 시점)
│   └── gemini_client.py
├── config/
├── core/
│   ├── data_fetcher.py
│   ├── regime_engine.py
│   ├── technical_analysis.py
│   ├── position_manager.py
│   ├── websocket_watcher.py  # Profit Guard v2
│   ├── cycle_logger.py
│   └── scheduler.py
├── exchange/
│   ├── bybit_client.py
│   └── paper_executor.py     # Paper trading
└── utils/
    └── telegram_bot.py
```

---

