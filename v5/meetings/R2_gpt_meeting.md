# R2 회의록 — 봇 frameworks + multi-agent + risk engine + hyperopt

**일시**: 2026-05-28
**참여**: Claude + GPT-5.5 Pro

---

## Q1: Framework 선택 — **Hybrid** 합의

**Core v4**: 순수 signal/risk 라이브러리 (자체 작성)
**Jesse**: research/backtest adapter만 (look-ahead 무, multi-TF, fee/funding/slippage 포함 fill sim, WFO batch runner)
**v3**: live execution adapter (Bybit client, position_manager, websocket_watcher, order executor, secrets/config, audit log)

**GCP micro에는 Jesse live X**. backtest는 로컬/배치.

**최대 위험**: Jesse sim ↔ v3 live 괴리. **반드시 parity test 작성**: 동일 core_signal / risk_engine / order contract 공유. candle/position/order 상태 일치 자동 검증.

---

## Q2: Multi-Agent — **2 LLM + 1 deterministic** 합의

| Layer | 역할 | 비고 |
|---|---|---|
| **TechnicalSetupAgent** (LLM) | OHLCV/MTF/volatility/liquidity/regime 보고 *LONG/SHORT 한 번에* 평가 | v3 2-prompt 분리 *폐기* |
| **SupervisorGateAgent** (LLM) | data freshness / contradiction / missing field / critical_reject / no-trade 강제 | 수익 극대화 X, *no-trade 강제 + hallucination 억제* |
| **DeterministicRiskEngine** (코드) | order 전 L1/L2/L3 check 독립 수행, LLM veto | R1 원칙 7 |

**비용 최적화 (운영 기본)**:
- **1 Flash call에 Technical + Supervisor 순차 섹션**으로 통합
- setup prefilter 미통과 → Gemini 호출 X (no-setup cycle 비용 $0)
- 1 호출 비용 ~$0.0005-0.003/cycle
- 7-role multi-agent는 비용/지연/과적합으로 **배제**

---

## Q3: Reflective Loop — **adopt, 룰 자기수정 금지**

- **반영 대상**: setup_id × regime × time_bucket 별 n, winrate, expectancy, profit factor, MAE/MFE, slippage, fee/funding, stop 원인, time-stop 원인, LLM confidence calibration
- **반영 금지**: prompt 문구, A급 setup 정의, critical_reject 기준, indicator threshold, leverage, sizing, SL/TP 룰의 *live 자동수정*

**= append-only telemetry**. Lessons 문서/룰 자기수정 X.

**구현**: trade_journal에 closed trade마다 (feature snapshot + LLM JSON + risk decision + order/fill + outcome) append. aggregator가 rolling 통계 계산. live code는 이 통계로 *룰 안 바꿈*. 통계는 walk-forward + paper acceptance에만 사용. 추가 LLM reflection 호출 X.

---

## Q4: Risk Engine 임계값 (구체적)

### L1 Trade-level
| 항목 | 값 |
|---|---|
| SL distance | **0.35-1.25%** (15m ATR 0.6-1.8배 sanity check) |
| TP | **1.3-1.6R**, min RR 1.2 (미만 = no-trade). 대략 0.45-2.0% |
| risk per trade | **≤ 0.25% equity** (METIS v3 1.5% margin 대비 6배 보수) |
| notional exposure | ≤ 50% equity (paper/initial), ≤ 75% (검증 후) |
| leverage | **2x default, 3x absolute hard cap** (METIS v3 5x → 절반 이하) |
| time-stop | **2h hard** (도달 시 market flat, 연장 X) |
| trailing | 기본 **off**. WFO 통과 시 *+1R 이후*에만 on |

### L2 Strategy-level
| 항목 | 값 |
|---|---|
| max_consecutive_losses | **3** → strategy kill until next UTC day |
| confidence threshold | **≥ 0.75 (75/100)**, pass/fail gate. sizing 용도 X |
| cooldown | 1 loss → 30분 / 2 losses → 2h / 3 losses → 다음 UTC day |

### L3 Account-level
| 항목 | 값 |
|---|---|
| daily loss | **-1.0%** → account kill (검증 후 max -2.0%) |
| weekly loss | **-3.0%** → weekly kill + manual review |
| max drawdown | **-5.0%** hard stop / **-8.0%** 전략 폐기/재검증 |
| total exposure | SOL 단일만. pyramiding/hedge/avg down 금지 |

### Kill-switch 테스트 계획
- Paper에서 synthetic loss injection으로 *각 layer 강제 trigger*
- kill 상태가 file/DB persist (프로세스 재시작 후에도 주문 차단)
- manual reset 없이는 복구 X
- 모든 reject: timestamp + layer + reason + attempted_order audit log

---

## Q5: Hyperopt 정책

### 허용 (coarse grid only)
- TP_R: 1.2 / 1.5 / 1.8
- time_stop: 1h / 1.5h / 2h
- ATR sanity envelope (filter용, stop generator X)
- max_spread_bps / min_liquidity / slippage assumption
- trailing on/off (binary, WFO 통과 시만)

### 금지
- **LLM prompt 자체** (in-sample 언어 과적합 + 재현성 붕괴)
- A급 setup 정의, critical_reject 기준, confidence threshold
- SL as free percent / ATR stop generator (구조적 invalidation 원칙 충돌)
- indicator threshold mining (RSI/MACD/volume/wick)
- leverage, martingale, averaging, loss-recovery sizing
- hour-of-day blacklist *표본에서만* 마이닝 (CPI/FOMC/funding window 같은 *사전 정의*만 허용)

### Walk-forward 프로토콜
- setup taxonomy + prompt freeze
- chronological rolling WFO
- 60-90일 calibration / 14-30일 validation / 14-30일 OOS test
- fee/funding/slippage/latency 가정 포함
- best **OOS expectancy + max drawdown + trade count + profit factor 안정성**으로 평가 (in-sample PnL X)
- untouched holdout + paper 통과 전 실전 X

---

## R3/R4 미해결

- **R3**: v4 setup taxonomy, feature schema, Gemini final JSON contract, Jesse↔v3 adapter parity test 설계
- **R4**: paper acceptance 기준, exact data source, slippage/funding model, deployment state machine, manual reset governance, audit log schema

---

## METIS v3 대비 큰 변경 (요약)

| 항목 | v3 | v4 합의 |
|---|---|---|
| Leverage | 5x | **2-3x** |
| Risk/trade | 1.5% margin | **0.25% equity** (실질 6배 보수) |
| Prompt | 2-prompt (LONG/SHORT 분리) | **1 prompt 통합** |
| Trailing | 기본 활성 (peak +0.6%, 0.3pp) | **기본 off**, WFO 통과 시 조건부 |
| Daily kill | 없음 | **-1% (검증 후 -2%) hard** |
| Consecutive losses | 없음 | **3 연패 → UTC 다음날까지 stop** |
| Time-stop | 없음 | **2h hard** |
| Reflective | 없음 | append-only telemetry (룰 수정 X) |
