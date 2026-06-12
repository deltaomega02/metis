# METIS v4 — 종합 시스템 설계 (R5 초안)

R1~R4 합의 종합. Claude 1차. GPT 최종 검증 대기.

---

## 1. 시스템 개요

**목표**: Bybit SOL-PERP 15m 단타 paper trading. 검증 후 실전. Gemini Flash 단일 + GCP micro + SOL 단독 + 수정 안하는 완벽한 모델.

**핵심 철학** (R1 7 원칙):
1. 확률-표본 (setup군 expectancy + WFO만 평가)
2. No-trade default / A급 setup only
3. 구조적 invalidation
4. 깨끗한 손실
5. 단순 청산 (fixed default, trailing 조건부)
6. 레짐·시간·이벤트 필터
7. 하드 리스크 엔진 (코드)

---

## 2. 아키텍처

```
┌───────────────────────────────────────────────────────────┐
│                  GCP micro (e2-micro)                     │
│  ┌────────────────────────────────────────────────────┐   │
│  │ stream_worker (async)                              │   │
│  │  - Bybit WS: orderbook 200ms, ticker, public trade │   │
│  │  - 5s/30s aggregates: spread, OBI, slippage, depth │   │
│  │  - in-memory, raw 저장 X                            │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │ cycle_runner (15m anchored)                        │   │
│  │  Phase 1: feature_builder                          │   │
│  │   - data_fetcher (kline 15m/1h/4h, funding, OI, …) │   │
│  │   - technical_indicators (EMA/RSI/MACD/ADX/ATR/BB) │   │
│  │   - level_extractor (24h/3d swing H/L, AVWAP)      │   │
│  │   - derivatives_extractor (funding zscore, OI∆, …) │   │
│  │   - context_collector (BTC/ETH 7f, event YAML)     │   │
│  │   - normalizer (ATR multiple, rolling zscore)      │   │
│  │   → feature_snapshot (immutable, hash)             │   │
│  │                                                     │   │
│  │  Phase 2: pre_filter (deterministic)               │   │
│  │   - data_quality / event_filter / position_state   │   │
│  │   - 미통과 → NO_TRADE skip (Gemini 호출 X)          │   │
│  │                                                     │   │
│  │  Phase 3: gemini_call (1 call, temp 0.2)           │   │
│  │   - Technical proposal + Supervisor review         │   │
│  │   - response_schema enforced                       │   │
│  │   - retry 1회 (validation fail시)                   │   │
│  │                                                     │   │
│  │  Phase 4: semantic_validator                       │   │
│  │   - price invariant (LONG: inv<ref<tgt)            │   │
│  │   - tick_size 반올림, RR 재계산 ±0.15               │   │
│  │   - 실패 → risk_engine VETO + LLM_OUTPUT_INVALID   │   │
│  │                                                     │   │
│  │  Phase 5: risk_engine (deterministic 3-layer)      │   │
│  │   - L1 trade: SL distance / TP R / size / leverage │   │
│  │   - L2 strategy: consecutive losses / confidence   │   │
│  │   - L3 account: daily / weekly / DD / exposure     │   │
│  │   - VETO 가능. KEEP/PASS만 order 진행              │   │
│  │                                                     │   │
│  │  Phase 6: order_executor (v3 코드 재사용)           │   │
│  │   - paper: paper_executor                          │   │
│  │   - live: bybit_client                             │   │
│  │   - SL/TP는 set_trading_stop으로 거래소 등록        │   │
│  │                                                     │   │
│  │  Phase 7: telemetry_writer (append-only)           │   │
│  │   - trade_journal (모든 필드, prompt_hash 포함)     │   │
│  │   - feature_snapshot은 ref만 (별도 archive)        │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │ position_monitor (15s polling, 이미 진입 시)        │   │
│  │  - mark_price 추적                                  │   │
│  │  - time_stop (2h) 강제                              │   │
│  │  - 거래소 SL/TP 체결 시 outcome 기록                │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │ daily_aggregator (UTC 00:01)                       │   │
│  │  - confidence bucket × hit rate / expectancy_R     │   │
│  │  - Brier / ECE / reliability                       │   │
│  │  - setup × regime split                            │   │
│  │  - daily report (Telegram or file)                 │   │
│  └────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘

         ┌────────────────────────────────────────┐
         │ 로컬 / 별도 batch (GCP micro 외부)      │
         │  - jesse_backtest_adapter              │
         │  - walk_forward_runner (60-90/14-30)   │
         │  - feature ablation                    │
         │  - hyperopt (제한된 coarse grid)        │
         └────────────────────────────────────────┘
```

---

## 3. 코드 모듈 (v4/code/)

```
code/
├── main.py                       # cycle_runner entry + 15m anchor scheduler
├── config/
│   ├── settings.py               # GCP/Bybit/Gemini/Risk 임계값
│   ├── events.yaml               # FOMC/CPI/SOL unlock 등 (주 1회 수동 update)
│   └── secrets.env               # API keys (gitignore)
├── core/
│   ├── feature_builder.py        # Phase 1 통합
│   ├── data_fetcher.py           # Bybit kline + funding + OI + liq
│   ├── technical_indicators.py   # EMA/RSI/MACD/ADX/ATR/BB
│   ├── level_extractor.py        # 24h/3d/swing H/L, session AVWAP
│   ├── derivatives_extractor.py  # funding zscore, OI delta, liq imbalance
│   ├── context_collector.py      # BTC/ETH 7f, event filter
│   ├── normalizer.py             # ATR multiple, rolling zscore
│   ├── stream_worker.py          # WS 5s/30s aggregate (in-memory)
│   ├── pre_filter.py             # Phase 2
│   ├── semantic_validator.py     # Phase 4 (price invariant 등)
│   ├── risk_engine.py            # Phase 5 (3-layer VETO)
│   ├── order_executor.py         # Phase 6 (paper + live adapter)
│   ├── position_monitor.py       # 15s polling, time_stop
│   ├── telemetry_writer.py       # Phase 7 append-only
│   └── daily_aggregator.py       # UTC 00:01
├── ai/
│   ├── gemini_client.py          # Phase 3 (response_schema, retry)
│   ├── prompt_builder.py         # system + static + few-shot + runtime
│   ├── response_schema.py        # JSON schema (Pydantic)
│   ├── static_blocks/
│   │   ├── system_instruction.txt
│   │   ├── seven_principles.md
│   │   ├── five_setups.md
│   │   ├── fifteen_critical_rejects.md
│   │   ├── supervisor_rule.md
│   │   └── few_shot_cards.md
│   └── prompt_version.py         # hash 생성 + version log
├── exchange/
│   ├── bybit_client.py           # v3에서 재사용 + 정리
│   ├── bybit_ws.py               # v3 재사용
│   └── paper_executor.py         # v3 재사용
├── adapters/
│   ├── jesse_backtest_adapter.py # 로컬 backtest에서 v4 core 호출
│   └── adapter_parity_test.py    # Jesse sim ↔ v3 live 일치 검증
├── observability/
│   ├── dashboard.py              # Streamlit
│   └── alerts.py                 # Telegram / email
└── tests/
    ├── test_semantic_validator.py
    ├── test_risk_engine.py
    ├── test_pre_filter.py
    └── test_kill_switch.py       # synthetic loss injection
```

---

## 4. 핵심 임계값 (R2 합의)

### L1 Trade
- SL distance: 0.35-1.25% (15m ATR 0.6-1.8배 sanity)
- TP: 1.3-1.6R, min RR 1.2
- risk per trade: ≤ 0.25% equity
- notional: paper ≤50% / 검증 후 ≤75%
- leverage: 2x default, 3x hard cap
- time-stop: 2h hard

### L2 Strategy
- max consecutive losses: 3 → strategy kill until next UTC day
- confidence threshold: ≥ 0.75 (pass/fail gate)
- cooldown: 1 loss 30분 / 2 losses 2h / 3 losses 다음 UTC day

### L3 Account
- daily loss: -1.0% (검증 후 max -2.0%)
- weekly loss: -3.0%
- max drawdown: -5.0% (전략 폐기 -8.0%)
- exposure: SOL 단일, pyramiding/hedge/avg-down 금지

---

## 5. LLM Prompt (R4)

- Model: gemini-3.5-flash
- Temperature: 0.2
- Native response_schema (Pydantic)
- 1 call: Technical proposal + Supervisor review (asymmetric veto)
- Retry: 1회만, 실패 시 NO_TRADE force
- Cached_content로 static block 고정 (비용 ↓)

---

## 6. Walk-Forward 검증 (R2/R3)

- 60-90일 calibration / 14-30일 validation / 14-30일 OOS
- min 30 trades/setup/period (Wilson CI)
- chronological rolling
- fee/funding/slippage/latency 가정 포함
- 평가: OOS expectancy / max DD / trade count / profit factor *안정성* (in-sample PnL X)
- untouched holdout + paper 통과 전 실전 X

---

## 7. Paper Pass/Fail 기준 (제안 — R5에서 확정 필요)

- 누적 60일 paper 통과 조건:
  - net expectancy_R after fees+funding > 0.10
  - profit factor > 1.3
  - max drawdown < 5%
  - ECE < 0.15
  - schema/semantic fail rate < 1%
  - 0.8+ confidence bucket expectancy_R > 0
  - min 100 trades

---

## 8. Model Kill-Switch (R4)

- schema/semantic fail rate > 1%
- p95 latency > cycle budget
- N≥100, ECE > 0.15
- N≥30, 0.8+ bucket expectancy_R ≤ 0
- daily -1% / 3 연패

→ paper-only or forced NO_TRADE 전환. 재검증 전 live 금지.

---

## 9. R5에서 GPT 최종 검증 받을 항목

1. 위 아키텍처 다이어그램에 *빠진 컴포넌트*?
2. 코드 모듈 분리가 *너무 세분* or *너무 큼*?
3. Paper pass/fail 기준이 *너무 빡빡* or *너무 느슨*?
4. Walk-forward 구간 분할이 SOL 단타에 *실제로 합리적*?
5. Cached_content 사용이 GCP micro에서 *예상되는 비용 절감*?
6. 빠진 *failure mode*? (네트워크 단절, 거래소 incident, Gemini quota 등)
7. 최종 prompt 원문 작성을 GPT에 *위임*할까, 우리가 작성 후 GPT 검토할까?
