# R4 회의록 — LLM prompt 디자인 최종

**일시**: 2026-05-28
**참여**: Claude + GPT-5.5 Pro

---

## Q1: Temperature = **0.2**
FinCoT 권장. adversarial reasoning + native response_schema 환경에서 0.0보다 반대증거 탐색이 낫다. self-consistency 추가 호출은 GCP micro/Flash 단일 목표와 충돌하므로 단일 호출 + 0.2.

---

## Q2: Schema enforcement 깊이

### Schema에서 강제
- 넓은 numeric range: confidence 0.00-0.95, target_R 1.0-3.0, time_stop 15-120, price>0
- enum: decision, setup_id (5+NONE), regime, volatility_regime, event_filter_state, btc_eth_context_state, critical_reject
- array max: critical_rejects 3, supporting_evidence 3, opposing_evidence 3, targets 2

### Code에서 강제 (semantic validator)
- tick_size 반올림, ATR multiple, min stop distance, NaN/inf
- ENTER confidence threshold
- **Price directional invariant** (LONG: invalidation < reference < target)

### 15 critical_reject 최종 명단 (R3 명단 → 보강)
```
DATA_QUALITY_FAIL / EVENT_FILTER_BLOCK / BTC_ETH_CONTEXT_CONFLICT /
REGIME_SETUP_MISMATCH / VOLATILITY_UNTRADABLE / LIQUIDITY_SPREAD_BAD /
FUNDING_OI_NOT_CONFIRMING / TAKER_FLOW_CONFLICT / NO_CLEAR_LEVEL /
NO_STRUCTURAL_INVALIDATION / RR_BELOW_MIN / ENTRY_CHASING_OR_LATE /
RANGE_LOCATION_BAD / LIQUIDATION_CONTEXT_ABSENT / MULTI_SIGNAL_CONFLICT
```

---

## Q3: Reasoning fields (허용 + 제한)

| 필드 | max chars |
|---|---|
| reason_short | 160 |
| technical_evidence_summary | 220 |
| supervisor_opposing_evidence | 220 |

**금지**: `market_story` 같은 이름 (narrative/CoT 유도). 짧은 evidence summary만 허용. risk engine은 이 텍스트를 *실행 조건으로 쓰지 않음*.

---

## Q4: Setup Taxonomy 5종 — 구체 정의

### 1. TREND_PULLBACK
- **Entry**: 1h/4h trend 동방향 + 15m가 이전 S/R, VWAP/EMA, demand/supply, FVG, ATR band 근처 pullback. 15m rejection + HL/LH 형성 + taker/volume 확인 + BTC/ETH 비충돌. *중간 추격 진입 금지*.
- **Invalidation**: LONG = pullback swing low / demand low 하회 + tick or 0.1-0.25 ATR buffer. SHORT mirror. *구조 레벨 없이 ATR stop만은 reject*.
- **Common rejects**: REGIME_SETUP_MISMATCH, ENTRY_CHASING_OR_LATE, NO_STRUCTURAL_INVALIDATION, TAKER_FLOW_CONFLICT, BTC_ETH_CONTEXT_CONFLICT

### 2. BREAKOUT_RETEST
- **Entry**: 명확한 range/compression/HTF level 돌파 + 15m close + volume/taker/OI 확장 + broken level retest hold. breakout candle 추격 금지. 다음 HTF level까지 target_R 확보 필요.
- **Invalidation**: LONG = broken resistance 아래로 재진입 / retest low 깨면 invalid. SHORT mirror. tick/ATR buffer.
- **Common rejects**: ENTRY_CHASING_OR_LATE, NO_CLEAR_LEVEL, RR_BELOW_MIN, FUNDING_OI_NOT_CONFIRMING, MULTI_SIGNAL_CONFLICT

### 3. RANGE_REVERSION
- **Entry**: 1h/4h neutral/range + range 상하단 touch 충분 + 가격 range extreme/sweep 지점. 15m reversal + absorption/taker flip 확인 → mid or 반대편 range로 target. *range middle 진입 금지*.
- **Invalidation**: LONG = range low / sweep low 하회. SHORT mirror. range 불명확 시 reject.
- **Common rejects**: REGIME_SETUP_MISMATCH, RANGE_LOCATION_BAD, VOLATILITY_UNTRADABLE, TAKER_FLOW_CONFLICT, BTC_ETH_CONTEXT_CONFLICT

### 4. FUNDING_OI_SQUEEZE
- **Entry**: funding extreme + OI/crowding 상승 + 구조 레벨에서 crowded direction 실패 + taker flow flip *동시*. positive funding + trapped longs → SHORT. negative + trapped shorts → LONG. confirmation 전 예측 진입 금지.
- **Invalidation**: LONG = failed breakdown / squeeze trigger low 하회. SHORT mirror. **funding/OI 변화만으로 stop 정의 X — 반드시 price-based structural invalidation**.
- **Common rejects**: FUNDING_OI_NOT_CONFIRMING, NO_CLEAR_LEVEL, TAKER_FLOW_CONFLICT, BTC_ETH_CONTEXT_CONFLICT, EVENT_FILTER_BLOCK

### 5. LIQUIDATION_SWEEP_REVERSAL
- **Entry**: prior high/low or visible liquidity pool sweep + liquidation spike + 빠른 reclaim/failure + absorption/taker reversal. BTC/ETH가 같은 방향으로 강하게 확장 중이면 discount or no-trade.
- **Invalidation**: LONG = sweep low 하회. SHORT mirror. sweep extreme 깨고 acceptance 시 invalid.
- **Common rejects**: LIQUIDATION_CONTEXT_ABSENT, ENTRY_CHASING_OR_LATE, NO_STRUCTURAL_INVALIDATION, VOLATILITY_UNTRADABLE, MULTI_SIGNAL_CONFLICT

---

## Q5: Few-shot 10개 (압축 decision-card 표 형식)

| 분류 | 개수 |
|---|---|
| ENTER (setup별 1개씩) | 5 |
| NO_TRADE | 3 |
| CRITICAL_REJECT trap | 2 |

**형식**: full JSON 예시 *금지*. 압축 decision-card 표:
- 각 row: feature pattern, expected decision/setup, invalidation style, main critical_reject, confidence band
- 실가격 긴 예시 = overfit → 상대적 feature + enum 중심
- ~1000-1500 tokens

**instruction-only 대안**: Flash + native response_schema = valid JSON은 ok. 단 semantic calibration / no-trade default / entry-reject 경계 약함. instruction-only는 latency/cost baseline용. paper candidate로는 비추천.

---

## Q6: Adversarial Supervisor — **Option C** (asymmetric veto)

### Schema 레이아웃
- top-level: R3 final fields 그대로 (risk engine 입력)
- `technical_proposal`: {candidate_decision, setup_id, direction, entry_ref, invalidation_ref, target_ref, confidence_raw, technical_evidence_summary}
- `supervisor_review`: {reviewed_candidate, opposing_evidence[], critical_rejects[], override_action, supervisor_opposing_evidence}

### Supervisor 가능 action (asymmetric)
- `KEEP` — Technical 결정 유지
- `DOWNGRADE` — confidence 낮추거나 NO_TRADE로 변경
- `VETO_NO_TRADE` — 강제 NO_TRADE

### Supervisor *금지*
- NO_TRADE → ENTER 변경 X
- LONG ↔ SHORT 뒤집기 X
- 새 setup 생성 X

→ **upgrade 불가** (hallucinated reversal 차단)

**Hard reject 조건** (어느 것이든 → final NO_TRADE):
- 구조적 invalidation 부재
- RR 부족
- data / event / BTC_ETH conflict
- price invariant 실패

---

## Q7: Invalid JSON Retry

- **max_retries**: 1
- Retry prompt: 동일 system / response_schema / feature_snapshot + 다음 한 문장 추가:
  > "Previous response failed validation: ${validator_errors}. Re-evaluate from the supplied features, do not repair the old text. Return exactly one schema-valid JSON object. If any market condition is uncertain, choose NO_TRADE. Do not output anything outside JSON."
- Fallback: NO_TRADE force + `risk_engine_veto_reason=LLM_OUTPUT_INVALID`
- **schema/transport 오류만 retry**. semantic invariant 실패는 retry보다 risk_engine VETO.

---

## Q8: Confidence Calibration 운영

### Telemetry
- 실시간 dashboard + daily summary + weekly report
- confidence bucket (0.60-0.70 / 0.70-0.80 / 0.80+) × hit rate / expectancy_R after fees+funding+slippage / Brier score / ECE / reliability diagram
- setup × regime × direction 분할
- no-trade rate, veto rate
- p50/p95 latency, token/cost
- min 30 trades/setup/period + Wilson CI

### Miscalibration 대응
- **live prompt에 calibration prior 입력 금지** (룰 자기수정 차단)
- 표본 부족 → paper 연장
- 충분한 표본에서 high-confidence bucket 비단조 / ECE>0.15 / 0.8+ bucket expectancy_R≤0 → **prompt/model candidate 폐기**
- threshold/gate 변경 → 새 version으로 간주, walk-forward/paper 처음부터

### Model kill-switch
- schema/semantic fail rate >1%
- p95 latency가 cycle budget 초과
- N≥100, ECE>0.15
- N≥30 0.8+ bucket expectancy_R≤0
- daily -1% / 3 연패

→ paper-only or forced NO_TRADE 전환, 재검증 전 live 금지

---

## Q9: Telemetry (append-only audit log)

### 필수 필드
- cycle_id, asof_utc, mode (PAPER|LIVE)
- symbol, venue, contract_spec, tick_size, lot_size
- bar_close_utc, timeframe_anchor, data_watermarks
- **prompt_version, prompt_hash, fewshot_version, schema_version, response_schema_hash**
- model_id, model_version, temperature, generation_config, GCP_region
- **feature_snapshot_hash + immutable_feature_snapshot_ref** (재현 가능)
- llm_request_id, llm_input_tokens, llm_output_tokens, llm_latency_ms, cost_usd_estimate
- retry_count, finish_reason, safety_block_reason
- llm_full_json
- **schema_validation_status, semantic_validation_status, semantic_validation_errors, validator_version**
- **risk_engine_version, risk_engine_decision, risk_engine_veto_reason, risk_state_before_after**
- order_decision, order_intent_id, paper_fill_model_version, order_fill, slippage, fees, funding
- outcome_close_reason, realized_PnL, realized_R, MAE, MFE, time_in_trade
- setup_id, direction, confidence, confidence_bucket, regime, volatility_regime, critical_rejects
- env_meta: stream_lag, missing_bar_count, API_latency, CPU_mem, clock_skew_ms, network_errors

### 제거
- raw tick/orderbook/news blob (별도 archive + hash ref)
- chain-of-thought / market_story
- LLM 산출 position_size/leverage/qty/risk
- 매 cycle full stream_worker dump

---

## Q10: 최종 Prompt Structure

### 섹션 순서 (9 블록)
1. **system_instruction**: role, SOL-only scope, no-trade default, forbidden outputs, no CoT
2. **static_rules**: 7 principles + 5 setup taxonomy 압축
3. **static_rules**: 15 critical_reject 한 줄 정의
4. **static_rules**: supervisor veto-only override + confidence semantics
5. **few_shot_cards**: 10 압축 row
6. **runtime_anchor**: cycle_id, asof_utc, symbol, venue, bar_close_utc, watermarks
7. **runtime_features**: T0 15m/1h/4h + T1 funding/OI/liq/taker + T2 levels/ATR + T4 BTC/ETH/events
8. **runtime_controls**: data_quality, event_filter_state, current_position_state, read-only risk_state
9. **task**: schema-valid JSON 반환 (response_schema는 Gemini API 인자, prompt 반복 X)

### 토큰 추정
- 입력 3000-5000 (system 250-350 / taxonomy+reject 700-1000 / few-shot 1000-1500 / runtime 900-1600 / task 100)
- response_schema 인자 +600-1200
- 출력 500-900
- 15m × 96 calls/day × Flash 단일 = 비용 낮음

### 압축 전략
- response_schema는 API 인자만, prompt 반복 X
- few-shot decision-card 표
- ATR-normalized, derived features
- **Gemini cached_content**로 static block 고정 (비용 ↓)
- 문자열 max chars
- live prompt에 calibration prior / weekly stats / trade history 입력 X
- setup/reject 한 줄 정의

### System instruction (skeleton)
> 너는 SOL-PERP 15m paper trading decision engine이다. asof_utc 기준 제공된 derived features만 사용하고 raw tick/orderbook/news, position sizing, leverage, order quantity, risk_pct, chain-of-thought를 출력하지 않는다. 기본 결정은 NO_TRADE이며 5개 setup 중 하나가 구조적 invalidation, target_R, event/BTC/ETH/data 필터를 모두 통과할 때만 ENTER를 허용한다. Technical candidate를 만들고 Supervisor가 반대증거로 veto/downgrade한 뒤 response_schema에 맞는 JSON만 반환한다. 룰 수정 제안과 memory 반영은 금지다.

---

## R5에서 freeze 할 항목

- response_schema 최종 코드
- 15 critical_reject 한 줄 정의
- semantic validator pseudocode
- risk_engine VETO 코드
- paper pass/fail thresholds
- GCP micro 배포 runbook
- dashboard/alert 기준
- **최종 prompt 원문** (R6 코드 직전 freeze)
