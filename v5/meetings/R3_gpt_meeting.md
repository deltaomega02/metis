# R3 회의록 — 데이터 / feature schema / final JSON contract

**일시**: 2026-05-28
**참여**: Claude + GPT-5.5 Pro

---

## Q1: Stage 1 Feature Schema

**Tier 채택**: T0 + T1 + T2 + T4 (T3 stage 2로 연기)

### SOL T0 — market structure
- 15m/1h/4h: ret_1, ret_4
- ema 20/50/200 state, ema_distance_pct
- rsi14, macd_hist_norm, adx14, atr14_pct, bb_width_pct, volume_zscore

### SOL T1 — derivatives flow
- funding_current_8h, funding_avg_24h, funding_zscore_7d
- oi_change_1h_pct, oi_change_4h_pct, oi_zscore_7d
- liq_long_24h_usd, liq_short_24h_usd, liq_imbalance_24h
- taker_buy_sell_ratio_1h_4h

### SOL T2 — levels (ATR multiple로 정규화)
- dist_24h_high/low_atr, dist_3d_high/low_atr, dist_swing_high/low_atr
- dist_session_avwap_atr, above_session_avwap

### T4 — context + events
- BTC/ETH compact (Q3 schema)
- event_lockout_state, minutes_to_next_high_impact_event, kst09_utc00_window_flag

### 정규화 원칙
- 가격 거리는 **ATR multiple** 또는 bps
- 수급 변화는 pct_change 또는 **rolling z-score**
- LLM 입력 raw table 금지 — 반올림된 numeric만

### T3 예외 (조건부)
- 단 1개 허용 가능: 12h/24h liquidation heatmap top cluster
  - nearest_cluster_side, nearest_cluster_distance_atr, top_cluster_rank
  - **단**: 과거 WFO ↔ live 산식 *완전 동일*일 때만

### Stage 2 추가 후보 (OOS 검증 통과 시)
- CVD slope (5m/15m/1h aggregate)
- OBI 5s/30s median/percentile decay
- liquidation heatmap 12h/24h cluster distance
- spread/slippage/liquidity health
- **feature ablation 통과** 필수 (PF, maxDD, trade count, fee-adj expectancy 개선)

---

## Q2: Cycle = **15분 primary**

- 1H은 SOL 단타에 늦음, 5m는 fee + 노이즈 ↑
- 15m = 하루 96 cycle, 단일 Flash call 비용 감당 + GCP micro OK
- **실제 주문 빈도는 cycle X, setup 품질 + risk engine이 제한**

### Trigger-based supplement
- 초기 paper: 전체 진입의 **0-25% 상한**
- 비주기 trigger는 *추가 Flash 호출 X*
- 마지막 15m JSON이 `CONDITIONAL` + signal_valid_minutes 내일 때만 deterministic executor가:
  - entry_zone touch / breakout close / retest / OI 1h zscore 급등 / liquidation spike → 실행
- BTC/ETH contra shock / spread widening / event BLOCK → 즉시 cancel

### Stream worker
- Bybit 200ms orderbook push: raw 저장 **금지**
- GCP micro에서 in-memory로 5s/30s **집계 metric만**:
  - spread, depth imbalance, slippage proxy, data_quality, reconnect 상태
- LLM에는 cycle 단위 요약만 전달
- live 룰 자기수정 금지

---

## Q3: BTC/ETH context schema (각 자산 7 fields, 1h)

```
ret_1h_pct, ret_4h_pct, ema20_vs_ema50_state, rsi14, adx14, atr14_pct, market_shock_flag
```

- 용도: **alpha X, veto 또는 size reduction context**
- BTC + ETH 둘 다 SOL 진입 방향에 역행 or shock → supervisor reject 또는 risk engine 감산

---

## Q4: Macro / event marker

### Source
- Stage 1: **자체 YAML hardcode + 주간 manual update**
- TradingView / Forex Factory / Trading Economics는 cross-check만
- SOL unlock, network outage, exchange incident는 별도 수동 등록
- 자동 fetch는 paper 안정화 후 Stage 2

### Update cadence
- 매주 일요일 UTC 1회 수동 업데이트
- high-impact event D-1 + 당일 재확인
- exchange incident / network outage = 발견 즉시 hot update

### 이벤트 우선순위
1. FOMC rate decision, dot plot, Powell press
2. CPI
3. PCE / NFP / 고용·인플레 high-impact USD data
4. Fed speech
5. SOL major unlock / network upgrade / outage
6. exchange incident / API outage / stablecoin depeg
7. funding timestamps UTC 00/08/16
8. KST09 UTC00 daily reset

### KST09 / UTC00 처리
- 항상 NO_TRADE는 과함
- **UTC 23:55-00:10**: 신규 진입 금지
- **UTC 00:10-00:30**: size multiplier 0.5 또는 strong setup only
- 기존 포지션: 임의 청산 X, hard stop + time-stop만

### YAML 필드
```yaml
event_id: ...
type: FOMC | CPI | ...
impact: high | medium | low
start_utc: ISO-8601
block_before_min: int
block_after_min: int
size_multiplier: float (0.0-1.0)
source: ...
notes: ...
```

- LLM 입력: `event_filter_state`, `minutes_to_event`, `kst09_flag`만
- enforcement는 **deterministic risk engine**이 담당

---

## Q5: Walk-forward sample 충분성

- **Setup 1개당 period당 최소 30 trades**
- 14일 OOS 5건 = underpowered (결론 X). rolling 60-90일까지 늘리거나 setup family pool
- **그 5건에 맞춰 threshold 조정 = 과적합** (금지)

### SOL 거래 빈도 추정
- 15m cycle + 1h/4h context + no-trade default: **총 15-50건/월** (setup별 3-15건/월)
- 1H-only: 8-30건/월
- **80+/월 = overtrading 의심** (fee drag + 과적합)

---

## Q6: Final JSON Contract (Gemini Flash 출력)

### 핵심 필드 (전체 schema는 v4/design/json_contract_v1.md 별도 저장)

```json
{
  "schema_version": "r3_sol_perp_v1",
  "symbol": "SOLUSDT",
  "asof_utc": "ISO-8601",
  "input_snapshot_id": "feature bundle hash",
  "decision": "ENTER_LONG | ENTER_SHORT | NO_TRADE",
  "setup_id": "TREND_PULLBACK | BREAKOUT_RETEST | RANGE_REVERSION | FUNDING_OI_SQUEEZE | LIQUIDATION_SWEEP_REVERSAL | NONE",
  "confidence": 0.0,
  "regime": "trend_up | trend_down | range | high_vol | low_vol",
  "volatility_regime": "high | normal | low",
  "entry_plan": {
    "entry_type": "MARKET_ON_CYCLE_CLOSE | LIMIT_RETEST | STOP_BREAKOUT | NONE",
    "reference_price": null,
    "entry_zone_low": null,
    "entry_zone_high": null,
    "signal_valid_minutes": 15,
    "max_slippage_bps": null
  },
  "structural_invalidation_price": null,
  "target_price": null,
  "target_R": null,
  "time_stop_minutes": null,
  "critical_reject_matched": [],
  "supervisor_check_passed": true,
  "event_filter_state": "CLEAR | REDUCE_ONLY | BLOCK",
  "btc_eth_context_state": "aligned | neutral | against | shock",
  "data_quality_ok": true,
  "reason_short": "1 문장 (≤160 char), chain-of-thought 금지"
}
```

### Critical reject enum (15개)
DATA_STALE / DATA_GAP / SPREAD_TOO_WIDE / EVENT_LOCKOUT / KST09_LOCKOUT / BTC_ETH_CONTRA_SHOCK / FUNDING_EXTREME_AGAINST / NO_STRUCTURAL_INVALIDATION / RR_TOO_LOW / VOLATILITY_TOO_LOW / VOLATILITY_TOO_HIGH / LOSS_STREAK_STOP / DAILY_LOSS_LIMIT / OPEN_POSITION_CONFLICT / MODEL_UNCERTAIN

### Validation rules
- 엄격 JSON 파싱, 추가 키 거부
- symbol = `SOLUSDT`만
- NO_TRADE → setup_id=NONE, entry_type=NONE, invalidation/target/target_R/time_stop = null
- ENTER → supervisor_check_passed=true, critical_reject_matched=[], confidence≥0.60, setup_id≠NONE
- **LONG**: invalidation < reference_price < target
- **SHORT**: target < reference_price < invalidation
- target_R은 *가격으로 재계산한 R과 ±0.15 이내* (consistency check)
- time_stop_minutes 15-120, signal_valid_minutes ≤ 30
- event_filter_state=BLOCK / data_quality_ok=false / daily -1% / 3연패 / open position conflict → risk engine 무조건 veto

### LLM이 출력 *금지*
- position_size, leverage, order_qty, risk_pct
- raw tick / orderbook / news / sentiment echo
- 장문 분석, chain-of-thought

---

## R4로 넘길 항목

- 단일 Gemini Flash prompt에서 **Technical + Supervisor 한 호출 분리** 방식
- feature bundle 압축 순서
- setup taxonomy 최종 정의 (5 setup 각각의 entry/invalidation/reject 조건)
- critical reject checklist 운영 방식
- NO_TRADE / ENTER few-shot 예시
- confidence calibration (LLM 0.65 = 실제 65%인가?)
- invalid JSON retry 전략
- no self-modification 문구
- paper telemetry logging 포맷
