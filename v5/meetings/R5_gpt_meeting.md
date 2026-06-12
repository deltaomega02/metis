# R5 회의록 — 종합 시스템 설계 최종 검증

**일시**: 2026-05-28
**참여**: Claude + GPT-5.5 Pro
**판정**: **GO_WITH_CONDITIONS**

---

## 핵심 발견 — "수정 안하는 완벽한 모델"이라는 목표 자체에 대한 진단

GPT remaining_concerns 인용:
> "가장 큰 우려는 *과적합과 oversimplification의 동시 위험*. 5개 setup과 hand-coded reject가 edge를 만든다는 보장은 없고, 반대로 모든 위험을 rule로 막는다고 시장 비정상성이 사라지지도 않는다. … **목표는 영구 무수정 환상이 아니라 live v4 rule freeze와 새 버전 승격 절차의 엄격화다**."

= 운영자의 "수정 안하는 완벽한 모델" 욕망은 *환상*에 가깝다. 진짜 가치 있는 목표는:
- **live freeze** (운영 중 prompt/rule/schema 자기수정 절대 금지)
- **새 버전 승격 절차의 엄격화** (WFO/paper/replay/contract 테스트 통과 전 ACTIVE X)

이건 v3의 "Fix #18~#45 누적 반복" 문제를 정면으로 푸는 방법.

---

## Q1: 빠진 13 컴포넌트 (필수 추가)

R5 이전 7 phases는 *의사결정 파이프라인*이고, 실제 운영은 *운영 평면* (state, recovery, idempotency, reconciliation)이 추가로 필요.

| # | 컴포넌트 | 핵심 사양 |
|---|---|---|
| 1 | **StateStore/EventJournal** | SQLite WAL, 모든 결정/주문/체결/리스크 변경 append-only event_journal + state_snapshot 파생값 |
| 2 | **CycleScheduler/CycleLock** | 15m candle close + 5-15초 버퍼, cycle_id 단위 lock, 중복 cycle skip |
| 3 | **OrderJournal/IdempotencyLayer** | decision_id + deterministic clientOrderId, PREPARED→SUBMITTED→ACKED→PARTIAL→FILLED→CANCELLED 상태 전이, unique constraint |
| 4 | **ExchangeReconciler** | booting + 주기적 open positions/orders/fills 조회 → state 대조 → 불일치 시 safe mode |
| 5 | **ExchangeNativeProtectionOrderManager** ⭐ | entry 체결 직후 reduce-only SL/TP 조건부 주문 즉시 배치 + ack 확인. 실패 시 즉시 reduce-only close |
| 6 | **DataFreshnessWatchdog/WSGapDetector** | WS heartbeat / sequence gap / candle timestamp / REST backfill 검사. stale → DATA_QUALITY_FAIL |
| 7 | **TimeSyncGuard** | NTP/chrony offset 감시. >500ms 경고, >1s 신규 진입 중지 |
| 8 | **HealthCheck/Alerting/ManualKillSwitch** | heartbeat, structured log, Telegram alert, manual kill DB persist |
| 9 | **ConfigSecretVersionRegistry** | prompt/schema/model/validator/risk_config hash 모두 decision에 저장. API key Secret Manager, withdrawal 권한 금지 |
| 10 | **DBIntegrityBackupMigration** | SQLite integrity_check, WAL, daily backup, schema migration table |
| 11 | **ResourceGuard/ProcessSupervisor** | systemd/Docker restart, single instance lock, log rotation, disk free <10% 진입 금지 |
| 12 | **DecisionSnapshotStore/ReplayHarness** | LLM 입력 snapshot + pre_filter + Gemini raw + validator + risk decision 압축 저장. 동일 snapshot replay 시 동일 action 검증 |
| 13 | **CostAccountingEngine** | maker/taker fee, expected slippage, funding accrual, partial fill cost를 R + 계정% 단위로 기록. pass/fail은 post-cost 기준 |

### 핵심 운영 invariants
- **entry 체결 후 exchange-native SL/TP 미부착 시간 30초 초과 = hard violation** (1회라도 no-go)
- crash-after-submit 시 *blindly resubmit 금지*. clientOrderId로 exchange 조회 후 상태 확정
- orphan position 발견 시: matching journal 있으면 adopt, 보호주문 없으면 emergency SL 배치 또는 reduce-only close

---

## Q2: 코드 구조

**Verdict: appropriate**. 14개 파일 OK.
- **단일 process + asyncio TaskGroup** 권장 (multi-process 비권장)
- stream_worker / 15m scheduler / position_monitor / watchdog / daily_aggregator 같은 event loop
- Gemini/Bybit SDK가 blocking이면 async wrapper or 제한된 threadpool
- core = domain logic만
- ai = Gemini adapter / prompt registry / response schema만 (risk sizing X)
- exchange = Bybit + protection order manager + instrument metadata
- observability ≠ telemetry_writer + state_store 혼동 X
- tests = **contract / replay / chaos** 비중 ↑ (crash-after-submit, stale WS, malformed response, DB corruption 자동화)

---

## Q3: Paper Pass/Fail — **too_loose**, 강화

| 항목 | Claude 제안 | GPT 조정 |
|---|---|---|
| duration | 60일 | **90일** |
| expectancy_R after fees | 0.10 | 0.10 |
| profit factor | 1.3 | 1.3 |
| max drawdown | 5% | **4%** |
| ECE | 0.15 | 0.15 (단 표본 200+ 일 때만 gate) |
| schema/semantic fail rate | 1% | **0.5%** (retry 이후 최종) |
| 0.8+ bucket expectancy_R | >0 | **>0.05** (min 30 trades when gated) |
| min trades | 100 | **150** |

### Additional (GPT 추가)
- **min 90일 AND 150 trades** (둘 중 하나만 ≠ 충분). 미달 시 180일까지 연장, 그래도 미달 = setup이 *과도하게 selective*
- 모든 수익 지표 = **post fees + funding + realistic slippage + partial fill + rejected order opportunity cost**
- bootstrap 5% lower bound가 심한 음수 → no-go (point estimate만 보면 안 됨)
- **0 hard risk invariant violation**: SL widen / averaging / martingale / leverage cap 초과 / unprotected position — 1회라도 no-go
- entry 후 exchange-native SL/TP 미부착 30초+ 사례 = 0회
- daily kill 발생일 trading days의 5% 이하, weekly kill 0회 권장
- **상위 3 trades가 net PnL의 40% 초과 = no-go** (운 의존)
- setup별 OOS/post-paper expectancy 음수 → 그 setup만 비활성화 (전체 모델 억지 유지 X)

---

## Q4: WFO 분할 — adjust

| 항목 | Claude | GPT 조정 |
|---|---|---|
| 기본 split | 60-90/14-30/14-30 | **90/30/30** |
| 최소 folds | (미정) | **6** |
| 목표 folds | (미정) | **8-12** |
| roll step | (미정) | **OOS 길이 = 30일** |
| embargo | (없음) | **8-12 bars** (indicator lookback + time_stop 중첩) |
| setup별 30 trades | per-period | **전체 OOS folds 합산** or live enablement 기준 |

부족한 setup (특히 FUNDING_OI_SQUEEZE, LIQUIDATION_SWEEP_REVERSAL)은 **live disabled 또는 experimental**.

60/14/14는 *smoke test*용으로만.

---

## Q5: Cached_content

- 입력 token **25-45% 절감**, 전체 call **15-35% 절감** (조건 충족 시)
- 정적 block만 cache, market snapshot은 매 call 동적
- prompt_hash 변경 시 새 cached_content 생성, *ACTIVE 승격은 contract test 통과 후*
- TTL 만료 전 갱신, hit rate 95%+ 가능
- restart 시 cache miss OK (non-cached fallback)
- response_schema는 generation_config라 cache 비용 절감 미보장

---

## Q6: Failure Modes — 20개 (detection / response / test 명시)

핵심:
1. Bybit REST/WS outage / rate limit
2. Gemini API outage / quota / latency spike
3. 네트워크 단절 / DNS / TLS
4. NTP drift
5. Exchange incident / depeg / instrument spec change
6. SOL specific (validator, unlock, chain halt)
7. GCP micro OOM / disk full / kill -9
8. DB corruption
9. WS message gap
10. Partial fill / order reject / minNotional/tickSize 위반
11. Duplicate order / crash-after-submit
12. SL/TP protective order placement failure ⭐
13. Feature data anomaly (NaN/inf, impossible OHLCV)
14. Fee/funding/slippage model drift
15. Event YAML stale (7일+ 미update → conservative block)
16. Prompt/schema/model drift
17. Risk state mismatch (loss_streak/daily_PnL/cooldown 오류)
18. Startup orphan open position
19. API key permission / secret compromise (trade-only, withdrawal disabled, IP allowlist)
20. Margin mode/leverage/liquidation buffer drift

각 mode마다 chaos test 자동화.

---

## Q7: 최종 prompt 작성 방식 = **Option A**

- Claude가 R1~R5 합의 반영해서 **단일 원문 1차 작성**
- GPT는 **adversarial reviewer** (schema ambiguity, leakage, overfit prompt, missing reject, validator 불일치 공격)
- Claude 수정 1회 → 15+ golden decision-card 테스트 → contract test → semantic validator → replay test → ACTIVE 승격
- **이후 live 중 prompt/rule 수정 금지**. 변경 = 새 version + WFO/paper gate 전체 통과 필수

"섹션 분담 X — 수정 안하는 모델은 단일 책임자가 낫다"

---

## Go Conditions (9개, R6 진입 전 필수)

1. **R6 구현 전에 13 컴포넌트 추가** (특히 StateStore, OrderJournal, ExchangeReconciler, ProtectionOrderManager, WSWatchdog, TimeSyncGuard)
2. **Entry 후 exchange-native SL/TP 미부착 30초+ 허용 X** 테스트 통과
3. **Chaos test 통과**: restart recovery / crash-after-submit / WS gap / Gemini outage / DB corruption / disk full
4. **WFO 90/30/30 + 최소 6 folds (목표 8+)**, setup별 trade 부족 시 live disabled
5. **Prompt/schema/model/prompt_hash freeze**, live 중 자기수정 금지
6. **Paper 최소 90일 AND 150 trades**, post-cost 기준 adjusted criteria 통과
7. **실전 첫 단계 risk/trade 0.10-0.15%** (R2 합의 0.25%의 절반 이하)로 2-4주 ramp → 0.25% 검토 ⭐
8. **API key**: trade-only, withdrawal disabled, IP allowlist, secret rotation
9. **Event YAML weekly update SLA**, stale calendar 시 conservative block

---

## Remaining Concerns (해소되지 않는 위험)

- 5 setup + hand-coded reject가 *진짜 edge* 라는 보장 X
- 모든 위험을 rule로 막아도 시장 비정상성은 사라지지 않음
- Gemini Flash 단일 의존, Bybit microstructure 변화, SOL 이벤트 급변, 수동 YAML 누락, paper 표본 부족, backtest-live fill 괴리
- **"영구 무수정"은 환상**. 진짜 목표 = live freeze + 새 버전 승격 절차의 엄격화
