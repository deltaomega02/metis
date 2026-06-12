# R2 — 단타 자동매매 봇 현황 리서치 (1차 정리)

WebSearch 6건. 2026-05-28.

---

## 1. Hummingbot (오픈소스 market making 프레임워크)

- **Perpetual Market Making + Funding Rate Arbitrage** (v1.27.0, 2026) 중심
- 양쪽에 limit 깔기 — *METIS와 다른 카테고리* (단타가 아닌 spread capture)
- 단타 strategy 전용 제공 없음
- 차용 가치: market making infra (호가 추적, fill 관리) 정도

출처: [Hummingbot docs](https://docs.hummingbot.org/strategies/perpetual-market-making/), [v1.27.0 release](https://hummingbot.org/release-notes/1.27.0/), [Finestel 리뷰](https://finestel.com/blog/hummingbot-review/)

---

## 2. Freqtrade (오픈소스 + Hyperopt + FreqAI)

- **Hyperopt** = Optuna 기반 strategy 파라미터 최적화. backtest 반복.
- **FreqAI (2026)** = 신규 데이터 흘러들면 자동 재훈련 모델/뉴럴넷.
- backtest + paper + live 모두 지원. Telegram/webUI 컨트롤.
- *전략 자체는 사용자 작성* (free-strategies repo 별도).
- 차용 가치: hyperopt + backtest 인프라, paper-live 통합 구조. *결정 엔진은 우리 LLM이 담당*.

출처: [Freqtrade Hyperopt docs](https://www.freqtrade.io/en/stable/hyperopt/), [GitHub repo](https://github.com/freqtrade/freqtrade), [Bitget 2026 가이드](https://www.bitget.com/academy/main-features-of-freqtrade-for-cryptocurrency-trading-in-america-2026-comprehensive-guide)

---

## 3. Jesse (Python 단타 / multi-TF 프레임워크)

- 300+ 지표 / multi-symbol + multi-TF / spot+futures *all native*
- **Look-ahead bias 없는 backtest** — walk-forward에 유리
- Smart order routing, partial fills, paper trading, Telegram/Slack/Discord 알림
- 단타 도구로 가장 성숙. METIS와 가장 가까운 카테고리.
- 차용 가치 큼: backtest engine, indicator suite, live infra. LLM을 *decision module*로 끼우기 좋음.

출처: [GitHub jesse-ai/jesse](https://github.com/jesse-ai/jesse), [jesse.trade](https://jesse.trade/), [docs](https://docs.jesse.trade/docs/research/backtest), [AI 통합 비교](https://medium.com/@gwrx2005/ai-integrated-crypto-trading-platforms-a-comparative-analysis-of-octobot-jesse-b921458d9dd6)

---

## 4. LLM Crypto Trading Agent — 2026 트렌드

**멀티에이전트 + Supervisor 패턴** (METIS v3는 single agent)

- 대표 구조 (7 role):
  - Fundamental / Sentiment / News / Technical Analyst
  - Bull Researcher / Bear Researcher
  - Risk Manager
- **Supervisor Agent**가 fact-check → hallucination 감소
- 멀티 프로바이더 (GPT-5.x, Gemini 3.x, Claude 4.x, Ollama local 지원)
- 규제 요구 (EU AI Act / SEC OCC 2026-13): **"Traceable Decision Chains"** + 인간 책임자 명시
- 인프라: decentralized GPU (Render / Bittensor)

**METIS v3와 차이**:
- v3는 *2-prompt bidirectional* (LONG/SHORT 별도 호출) = *얕은 멀티에이전트*
- 2026 트렌드는 *역할 specialist* (Technical / Risk / Sentiment 등)

출처: [KuCoin AI agents 2026](https://www.kucoin.com/blog/ai-agents-vs-llms-crypto-analysis-market-2026), [GPTrader 오픈소스 2026](https://gptrader.app/ai-trading/best-open-source-ai-trading-agents-github-2026), [FlowHunt LLM 봇 비교](https://www.flowhunt.io/blog/llm-trading-bots-comparison/), [VPS infra](https://www.vpsforextrader.com/blog/autonomous-trading-agents/)

---

## 5. 학술 — FinGPT / BloombergGPT / CryptoTrade

- **BloombergGPT**: 금융 sentiment 분류 우수, 일반 NLP도 경쟁력.
- **FinGPT**: 오픈소스 금융 LLM (sentiment / entity recognition / trading 결정).
- **CryptoTrade** (arXiv 2407.09546): *reflective LLM agent for zero-shot crypto trading*. 전통 전략 + time-series baseline 대비 *superior 수익률*.
- **WebCryptoAgent** (arXiv 2601.04687): web informatics 통합 에이전트.
- **Fact-Subjectivity Aware Reasoning** (arXiv 2410.12464): fact vs opinion 분리 reasoning.

**Reflective agent 패턴**: 거래 후 *자기 reasoning을 reflection*해서 다음 결정 개선. METIS v3엔 없음.

출처: [CryptoTrade arXiv](https://arxiv.org/html/2407.09546v1), [WebCryptoAgent](https://arxiv.org/html/2601.04687v1), [Fact-Subjectivity](https://arxiv.org/html/2410.12464v3), [FinGPT](https://ideas.repec.org/p/arx/papers/2307.10485.html), [FinAgent multimodal](https://personal.ntu.edu.sg/boan/papers/KDD24_FinAgent.pdf)

---

## 6. Risk Engine / Kill-Switch — 업계 best practice

**Daily Loss Threshold**: 2-5% 자본 (hard rule, soft warning X).

**Multi-Layered Kill-Switch** (독립 레이어):
- L1 **Trade-level**: stop loss, take profit, position size 제한
- L2 **Strategy-level**: max consecutive losses, confidence threshold
- L3 **Account-level**: daily loss, total exposure, drawdown
- *한 레이어 실패해도 다음이 잡음* — three chances

**Risk checks 순서**: 모든 check가 *order 전에* 실행. 위반 시 signal 거부 (거래소 도달 전).

**구조**:
- 전략 vs 리스크 *분리* (separation of concerns)
- *Independent layers* — 전략이 어떻게 행동해도 safeguard 유지

**테스트**:
- Paper trading에서 *kill-switch trigger 강제 테스트* 필수
- "정확히 설계대로 멈추는지" 확인. 안 멈추면 *live 금지*.
- 주간 human review 최소.

출처: [FIA 자동매매 risk controls](https://www.fia.org/sites/default/files/2024-07/FIA_WP_AUTOMATED%20TRADING%20RISK%20CONTROLS_FINAL_0.pdf), [Tickerly](https://tickerly.net/how-to-manage-riYOUR_OPENAI_API_KEY/), [Petr Vojáček overfitting](https://petrvojacek.cz/en/blog/trading-bot-risks-and-tools/), [Appinventiv 2026](https://appinventiv.com/blog/crypto-trading-bot-development/), [LuxAlgo risk](https://www.luxalgo.com/blog/riYOUR_OPENAI_API_KEY/), [3Commas AI bot risk](https://3commas.io/blog/ai-trading-bot-risk-management-guide)

---

## 차용/차별화 매트릭스 (R2 초안)

| 영역 | 차용 (어디서) | 자체 구축 / 차별화 |
|---|---|---|
| Backtest engine | **Jesse** (look-ahead 무, multi-TF) | 결정 모듈만 LLM 교체 |
| Live infra (WS, order, fill) | **Jesse** 또는 METIS v3 보존 | — |
| Hyperopt / 파라미터 최적화 | **Freqtrade Optuna** 사상 차용 | LLM prompt는 hyperopt 대상 X (오버피팅 위험) |
| Risk engine | **3-layer kill-switch** 업계 표준 | METIS v3는 L1+일부 L3만 → L2 추가 (max consecutive losses, confidence threshold) |
| Decision engine | — | **METIS 자체 LLM** (Gemini Flash) |
| Agent architecture | 2026 트렌드 *역할 specialist* | METIS는 *단순화 우선* → **2-3개 specialist만** (Technical + Risk Manager + Supervisor) |
| Reflective loop | **CryptoTrade 논문 패턴** | 거래 후 reflection prompt 추가 — *단 walk-forward용 통계 누적만*, 룰 자기수정 금지 (운영자 "v# 변경 금지") |
| Market making | Hummingbot | **차용 X** (METIS는 directional 단타) |

---

## R2 핵심 질문 (GPT에 회의)

1. **Jesse 차용 vs 자체 구축**: METIS v3 코드 보존하면서 Jesse infra 일부만 가져오는 게 합리적인가, 아니면 v4를 *Jesse strategy*로 처음부터 작성하는 게 빠른가?
2. **Multi-agent specialist 도입 깊이**: Gemini Flash 단일이 제약. 멀티에이전트 = 호출 N배. 단타 비용 vs 효과. *최소 specialist 수*는?
3. **Reflective loop 도입 여부**: CryptoTrade 패턴. 자기 룰 수정은 운영자 거부 패턴이므로 *통계 누적용*으로만. 어떻게 박는지.
4. **3-layer Risk engine 수치**: 단타 (보유 15분~2h) + Gemini Flash + SOL 단독 + Paper 시작 환경에서 *각 layer 임계값*은?
5. **Hyperopt 함정**: Freqtrade hyperopt = in-sample 최적화 → 과적합 위험. 우리 walk-forward 원칙과 충돌. *어떤 파라미터*만 hyperopt 허용?
