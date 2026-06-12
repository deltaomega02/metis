# METIS v4 — 합의된 7 핵심 원칙 (R1 산출물)

Claude + GPT-5.5 Pro 합의. 외부 트레이더 합의 (Raschke / Hougaard / Douglas / pro scalper consensus) 근거. METIS 과거 거래는 *결론 근거 아님*.

---

## 7 원칙

### 1. 확률-표본 원칙 [both: LLM + code]
단일 거래 승패가 아니라 **동일 setup군의 비용 차감 후 expectancy + walk-forward 결과**로만 시스템 평가.
- LLM prompt: "최근 n=2/n=9 손익으로 기준 변경 금지", "거래별 판단은 사전 체크리스트 + EV로만"
- Code: setup별 net R / profit factor / MAE 누적

### 2. No-trade default / A급 setup only [both]
구조 + 레짐 + 유동성 + trigger + R:R **중 하나라도 불명확하면 진입 X**.
- LLM prompt: *REJECT를 성공 출력으로 간주*. 거래 빈도 목표 X.
- Code: should_enter=false도 정상 KPI로 카운트

### 3. 구조적 invalidation 우선 [both]
Stop은 swing high/low / liquidity level / range boundary 등 **thesis 무효화 가격**.
- ATR = *buffer + min distance + position sizing 보조*만
- LLM prompt: invalidation 가격 + 그 이유를 *진입 전*에 출력
- Code: invalidation 가격 ↔ AI 출력 SL 일치 검증

### 4. 깨끗한 손실 [both]
Invalidation 발생 시 **즉시 청산**. stop widen / averaging down / martingale / "리스크 인정하나 진입" *전부 금지*.
- LLM prompt: 자기 합리화 reasoning 명시 차단 (METIS Fix #43 패턴 유지/강화)
- Code: SL 이동 = 유리 방향만 허용 (trailing 외 widening 차단)

### 5. 단순한 청산 [both]
**기본 = fixed target/stop/time-stop**. Trailing은 *1R 이후 + 구조 전환 이후 + 검증된 레짐에서만 조건부*.
- METIS Fix #44 trailing (peak +0.6% 활성, 0.3pp 후퇴) 재평가 → *조건부 활성화*로 전환
- LLM prompt: 청산 결정도 multi-condition (2+ 일치)
- Code: time-stop 추가 (2h 경과 + 무회복 = 청산)

### 6. 레짐·시간·이벤트 필터 [both]
SOL 단독이어도:
- BTC/ETH beta (BTC가 크게 움직이면 SOL 동조)
- 변동성 regime (high/normal/low/squeeze)
- Funding 정산 (UTC 00/08/16, *주의 시간*)
- KST 09:00 / UTC 00:00 daily reset
- FOMC / CPI / Fed / 대형 unlock / exchange incident

→ **no-trade or size reduction**
- LLM prompt: 레짐/이벤트 입력 + "이 setup은 현재 레짐에 허용되는가?" 판정 우선
- Code: 이벤트 시간 윈도우 자동 차단

### 7. 하드 리스크 엔진 [code only]
**LLM 외 코드 영역**:
- 거래당 0.5-1% 자본 (USD 손실액 기준, 구조적 stop + fee + slippage 포함)
- 일일 1-2% hard stop (도달 시 그날 거래 중단)
- 연속 N회 손실 → cooldown
- fee/slippage 포함 position sizing

---

## 우선순위 (v3 손실 원인 처방)

1. **#2 (A급 setup only)** — 진입 자리 엄격화
2. **#3 (구조적 invalidation)** — SL 정의 방식 전환
3. **#6 + refined TF (#7)** — 노이즈/비정상 구간 차단
4. **#7 (자본/일일 한도)** — 코드 강제
5. **#4 (즉시 손실 확정)** — sunk cost 차단
6. **#5 (trailing 조건부)** — churn 방지

---

## 다음 라운드에서 풀어야 할 것

- **R2** (봇 현황): entry/stop/TP/trailing/sizing/kill-switch *업계 best practice* + LLM I/O schema
- **R3** (데이터): SOL OHLCV + BTC/ETH context + funding/OI/liquidation/spread + macro event marker + net R 라벨 + walk-forward 분리
- **R4** (prompt): critical_reject 목록 + A급 setup checklist + invalidation 출력 + REJECT schema + temperature 0 + self-modification 금지
