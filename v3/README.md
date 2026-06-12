# METIS v3 (METIS-F2 단타 시스템)

암호화폐 무기한 선물 자동매매 시스템 — 3세대.

Bybit ETHUSDT + SOLUSDT + XRPUSDT 멀티 심볼. Gemini 3.5 Flash 기반 단타(15분~2시간 보유) + 완전 병렬 분석 + 점수 비교 진입.


---

## 개요

METIS v3는 2026-05-12 운영자 결정으로 *DB clear 후 완전 새 시작*. v2 결과 (-$3.47, Fix #18-25 누적) 폐기.

이후 *검증 v3* 진행 — 시드 추가 후 *9 거래 (W3 L6, net -$176)* 발생. 5/20 운영자 판단으로 + *단타 시스템 전면 재설계*:

- Pro → Flash (4-10배 빠른 응답)
- BTC 제외 → ETH + SOL + XRP (BTC 5/6 패배 패턴 회피)
- 단타 (15분~2h 보유, 0.3-0.6% 목표)
- Paper mode 전환 (2-3일 검증 후 실전)

5/21 *완전 병렬 + 점수 비교 진입* 적용. 현재 운영 중.

---

## 설계 철학

- AI = 판단자, 코드 = 실행자
- Multi-condition exits (단일 트리거 X — 2개 이상 동시 일치)
- 손실 차단 우선 (수익 추격보다 손실 방어)
- Paper trading 우선 검증 후 실전
- 단타 노이즈는 정상 (코드 trailing이 자동 처리)

---

## v2 대비 주요 변경 (5/12 ~ 5/21)

### Fix #26 ~ Fix #36 (5/12 ~ 5/19) — Prompt 전면 재작성
- prompts.py 3 함수 (LONG/SHORT/recheck) 통째 새로 작성
- XML 태그 구조 / Few-shot examples / Adversarial check
- Cardwell 추세 끝물 규칙 / EMA200 regime filter
- DI 방향 / Triangulation / Multi-TF 분기

### Fix #37 ~ Fix #40 (5/18) — 단타 진입 prompt 정교화
- Fix #38: 가지치기 + LOSS 가이드 + Strong setup 8 패턴
- Fix #39: J+C 조합 (Few-shot + Setup)
- Fix #40 P6: Default ENTER + LOSS 가이드 + counter-trend 특별 거부

### Fix #41 (5/20) — Recheck PQ Hybrid
- 12 recheck 100% HOLD 편향 발견
- PB Skeptical (지금 진입할까? 질문) + PK 추상화 패턴
- Pattern A-D (수익→손실 / 반대 캔들 / 느린 반전 / 연속 손실)
- 첫 작동: 거래 #8 BTC SHORT AI_EXIT

### Fix #42 (5/20 23:50) — 단타 모드 첫 적용
- PAPER_MODE=true
- Model: Gemini 3.5 Flash
- Primary timeframe: 15m
- next_recheck 클램프: 0.25~24h (이전 1~24h)
- 단타 가이드 patch

### Fix #43 v2 (5/21 01:30) — 진입 + recheck 통째 단타 재작성
- LONG/SHORT 진입 prompt 통째 새로 (리서치 기반)
  - 단타 도메인 (15분~2h, R:R 1.5+, 60% 승률 목표)
  - Classic vs Hidden Divergence 명확 구분
  - ADX peak 후 하락 = 추세 끝물 신호
  - Critical Rejects 4 (다이버전스, ADX peak, 거래량 약화, 반대 캔들) — 2+ 일치 시 거부
  - 자기 합리화 reasoning 차단 ("리스크 인정하나 보완" 패턴 거부)
- recheck 통째 새로 작성 — Scalping Position Manager
  - HOLD / EXIT만 (MODIFY 제거)
  - Multi-condition (2+ 동시 일치)
  - 시간 임계 표 (0~30분 / 30분~1h / 1h~2h / 2h+)

### Fix #44 (5/21 02:30) — 자체 Trailing Stop
- `core/trailing_stop.py` 신규
- 활성화: peak +0.6% margin 도달 시
- Trail 거리: peak에서 0.3pp 후퇴 시 SL 이동
- One-way (LONG 위로만, SHORT 아래로만)
- 30초 폴링 (Profit Guard와 같이)

### Fix #45 (5/21 14:30) — 완전 병렬 + 점수 비교 진입
- 3 심볼 동시 분석 (ThreadPoolExecutor, max_workers=N)
- LONG + SHORT도 심볼 내부에서 병렬
- 분석 시간: 65초 → ~29초 (55% 단축)
- 점수 비교 진입: 모든 후보 중 score 최고 + 동점 시 SYMBOLS 순서 (ETH > SOL > XRP)
- Winner는 cached strategy로 직접 진입 (AI 재호출 X)
- cycle_logger thread-safe (threading.local + take_and_reset/load_record)

### 기타 변경
- PG 임계 단타 조정 (ACTIVATION 5%→1.5%, MIN_PEAK 6%→1.5%, DRAWDOWN 50%→30%, HARD 10%→3%)
- 청산 후 cooldown 제거 — 즉시 3 심볼 분석
- 대시보드 동적 시드 인식 (paper_state.db INITIAL row 자동)

---

## 아키텍처

```
                     +---------------------+
                     |   Main Event Loop   |
                     |  (analysis ticks)   |
                     +----------+----------+
                                |
                  +-------------+-------------+
                  |  ThreadPoolExecutor       |
                  |  (3 심볼 완전 병렬)        |
                  +-------------+-------------+
                                |
        +-----------+-----------+-----------+
        |           |           |           |
      [ETH]       [SOL]       [XRP]      [...]
        |           |           |
   Phase 1: 데이터 수집 (Bybit kline + 지표)
   Phase 2: 레짐 분류 (룰 기반)
   Phase 3: AI 분석 (LONG + SHORT 병렬, Gemini Flash)
   Phase 3.5: 전략 검증 (leverage / SL / TP)
        |
        v
   +------------------+
   |   점수 비교        |  -> winner (score desc, 동점 시 SYMBOLS 순서)
   +--------+---------+
            |
            v
   Phase 4: 진입 (winner의 cached strategy로, AI 재호출 X)
            |
   +--------+----------+---------+----------+
   |        |          |         |          |
  SL/TP  WebSocket  Profit    Trailing   Recheck
 (거래소)  Monitor   Guard      Stop    (AI, 1h)
                   (30s 코드) (30s 코드)
```

역할 분담:
- 진입 결정: AI (Gemini Flash)
- Trail SL: 코드 (자체 자동, 30초)
- Drawdown 보호: Profit Guard v2 (30초)
- Liquidation 안전: Bybit 거래소
- Recheck (HOLD/EXIT): AI (1h, multi-condition)

---

## 주요 기능

- 완전 병렬 분석 (3 심볼 + LONG/SHORT 동시)
- 점수 비교 진입 (winner 선택)
- 자체 trailing stop (거래소 의존 X)
- Profit Guard v2 (Multi-TF reversal + peak drawdown + hard drawdown)
- Active Capital Guardian recheck (HOLD/EXIT only, multi-condition)
- Paper trading 모드 (운영 코드 그대로, 가상 체결)
- Telegram 알림
- Cycle log JSON (시장 데이터 + AI 추론 + 결정 풀 컨텍스트)
- Streamlit 대시보드 (동적 시드 인식)

---

## 프로젝트 구조

```
v3/
├── main.py                   # 이벤트 루프 + 병렬 분석 + winner 선택
├── ai/
│   ├── prompts.py            # 진입 (Fix #43 v2) + recheck (Fix #44)
│   └── gemini_client.py
├── config/
│   ├── settings.py           # 단타 임계 (Profit Guard / Recheck / Symbol)
│   └── logging_config.py
├── core/
│   ├── data_fetcher.py       # Bybit kline + 지표 계산
│   ├── regime_engine.py
│   ├── technical_analysis.py # EMA / RSI / MACD / ADX / ATR / BB
│   ├── leverage_calculator.py
│   ├── position_manager.py
│   ├── trailing_stop.py      # Fix #44 자체 trailing
│   ├── websocket_watcher.py  # Profit Guard v2
│   ├── scheduler.py
│   ├── cycle_logger.py       # Thread-safe (Fix #45)
│   └── trigger_monitor.py
├── exchange/
│   ├── bybit_client.py
│   ├── paper_executor.py     # Paper trading
│   └── bybit_websocket.py
├── utils/
│   └── telegram_bot.py
├── build_trades_csv.py
├── requirements.txt
└── README.md
```

---

## 설정 (config/settings.py)

```python
# 심볼 (병렬 분석 + 동점 시 우선순위)
SYMBOLS = ("ETHUSDT", "SOLUSDT", "XRPUSDT")

# 모델
MODEL_ID = "gemini-3.5-flash"

# Recheck 주기
DEFAULT_RECHECK_HOURS = 1

# Profit Guard (단타)
ACTIVATION_PCT = 0.015          # 활성화 1.5% margin
DRAWDOWN_RATIO_BTC = 0.30       # 피크 30% 후퇴 시 EXIT
DRAWDOWN_RATIO_ALT = 0.30
HARD_DRAWDOWN_MIN_PEAK = 0.03   # 피크 3%+ 도달
HARD_DRAWDOWN_ABS_PP = 0.015    # 절대 1.5pp 후퇴

# Paper Mode
PAPER_MODE = true
PAPER_INITIAL_BALANCE = 1294.97
```

Trailing Stop (`core/trailing_stop.py`):
```python
ACTIVATION_PNL_PCT = 0.6  # 활성화 +0.6% margin
TRAIL_DISTANCE_PP = 0.3   # 후퇴 0.3pp
CHECK_INTERVAL_SEC = 30
```

---

## 사용법

### Paper trading (검증)

```bash
# .env에 PAPER_MODE=true
python -u main.py
```

paper_executor가 mainnet 가격 기준 가상 체결. signed endpoint 호출 X. 상태는 `database/paper_state.db`.

### 실전 운영

```bash
# .env에 PAPER_MODE=false (mainnet API 키 확인)
set -a && source .env && set +a
setsid nohup ./venv/bin/python -u main.py > logs/output.log 2>&1 < /dev/null &
disown
```

---

## 검증 상태 (5/21 기준)

Paper mode 검증 중 — 거래 #12~#16 표본 5건:
- #12 SOL LONG: -$29.86 (SL hit, Fix #42)
- #13 ETH LONG: -$16.22 (AI_EXIT, Fix #42)
- #14 XRP SHORT: -$19.46 (SL hit, Fix #43 진입만 + recheck patch)
- #15 SOL LONG: **+$51.74** (TP hit, Fix #43 v2 첫 흑자, Hidden Bullish Div)
- #16 SOL LONG: **+$1.45** (trail SL lock, Fix #43 v2 + Fix #44 trailing)

Fix #43 v2 완전체 (#15 + #16): **2/2 흑자, +$53.19**

2-3일 페이퍼 검증 후 실전 결정.

---

## 데이터베이스

```
database/
├── metis_f2.db        # Live: futures_positions, position_rechecks
└── paper_state.db     # Paper: paper_balance, paper_positions, paper_orders
```

---

## Risk Warning

이 소프트웨어는 연구 등급이며 금융 자문이 아님. 암호화폐 무기한 선물은 고위험 레버리지 상품. 과거 성과가 미래를 보장 X.

잃을 수 있는 자본만 사용. Paper mode 충분히 검증 후 실전. 저자는 손실에 대한 책임 X.

---

