# R1 회의록 — 단타 트레이더 마인드 원칙

**일시**: 2026-05-28
**참여**: Claude (정리/제안) + GPT-5.5 Pro (반박/보완)

---

## 1. Claude 7 원칙 초안에 대한 GPT 판정

| # | 원칙 | 판정 | 핵심 보완 |
|---|---|---|---|
| P1 | 확률적 마인드 | **refine** | 추상 문구 X → 구체 instruction ("최근 n=2/n=9 손익으로 기준 변경 금지", "동일 setup군 장기 expectancy로만 평가") |
| P2 | 손실 cleanly/quickly | **agree** | "quickly"는 감정적 조기청산 아닌 *invalidation 도달 시 지체 없이 손실 확정*. stop widen / averaging down / "리스크 인정하나 진입" 금지 |
| P3 | 진입 정밀도 > 청산 영리함 | **agree** | "조금 괜찮은 자리" X. 구조·유동성·레짐·R:R *동시에 맞는 A급 setup만* |
| P4 | 트레일링 신중 | **refine** | 기본 = fixed target/stop/time-stop. trailing은 *1R 이후 + 구조 전환 이후 + 검증된 레짐에서만 조건부* |
| P5 | 구조적 invalidation | **agree** | ATR은 invalidation X. *buffer / min distance / position sizing 보조*만 |
| P6 | 자본 % 리스크 룰 | **agree** | *prompt 아닌 코드에서 강제*. 레버리지는 리스크 X — 진짜 리스크는 *USD 손실액* (구조적 stop + fee + slippage 포함) |
| P7 | 30m primary | **refine** | "30m > 15m" 컨센서스 X. 정확한 원칙: *30m/1h로 bias·regime + 15m 이하는 trigger*. net expectancy + walk-forward로 검증 |

---

## 2. GPT가 보완한 6개 누락 원칙

1. **No-trade default / overtrading 차단** — LLM은 *REJECT를 성공 출력으로 간주*. 거래 빈도 목표 X.
2. **비용 차감 후 expectancy** — 승률 X. net R / average win-loss / profit factor / MAE로 평가.
3. **Walk-forward / OOS 검증** — 과거 거래로 룰 확정 X. 기간 분리 + 레짐 분리 + forward shadow.
4. **Regime adaptation** — LLM은 *방향 예측 전에 현재 레짐이 해당 playbook에 허용되는지 판정*.
5. **Event/session filter** — FOMC/CPI/Fed/대형 unlock/exchange incident/funding 정산/KST 09:00 daily reset 전후 → no-trade or size reduction.
6. **사전 커밋** — entry 전 stop/target/time-stop/invalidation/no-add 모두 확정. loss aversion/sunk cost 제거.

---

## 3. v3 손실 진단 재확인 (R0의 "진입자리 c" 가설)

**P3 단독 강화로 충분?** → **불충분**.

**복합 원인 + 우선순위**:
1. P3 — A급 setup만 진입
2. P5 — 구조적 invalidation + R:R 선검증
3. P7_refined + regime/event filter — TF 노이즈 + 비정상 구간 차단
4. P6 — 거래당/일일 손실 한도 코드 강제
5. P2 — invalidation 후 즉시 손실 확정
6. P4 — trailing 기본값 폐기 / 조건부화

**GPT 인용**: "잦은 손절은 보통 *나쁜 entry + 구조 아닌 stop + noisy TF + 부적합 레짐 + 비용·trailing churn*이 결합. P3 단독으론 부족. 그러나 METIS 과거 거래만으로 확정하면 과적합."

---

## 4. R2/R3/R4에 넘길 미해결 질문

- **R2 (봇 현황)**: v3의 실제 entry trigger, stop/TP/trailing, position sizing, daily kill-switch, LLM I/O schema, 주문 타입, fee+slippage 모델 — 어떤 봇이 어떻게 풀고 있는지.
- **R3 (데이터)**: SOL 15m/30m/1h OHLCV + BTC/ETH context + funding/OI/liquidation/spread + KST 09 / US macro event marker + net R 라벨 + walk-forward 분리.
- **R4 (prompt 디자인)**: critical_reject 목록 / A급 setup checklist / structural invalidation 출력 형식 / REJECT를 정상 성공으로 만드는 schema / temperature 0 결정성 / self-modification 금지 문구.
