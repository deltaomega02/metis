# R4 — LLM trading prompt 디자인 리서치 (1차 정리)

WebSearch 6건. 2026-05-28.

---

## 1. Structured Output — 2026 production 표준

**Three levels**:
- L1: Prompt engineering (불안정)
- L2: Function calling / Tool use (개선)
- L3: **Native Structured Output** (최고) — constrained decoding + JSON Schema 100% valid

**원칙**:
- Reasoning first (답 commit 전 reasoning 강제)
- **One schema per task** (50+ field 하나면 분할)
- JSON 출력: **temperature 0.0-0.1**

출처: [TECHSY](https://techsy.io/en/blog/llm-structured-outputs-guide), [DEV pockit_tools](https://dev.to/pockit_tools/llm-structured-output-in-2026-stop-parsing-json-with-regex-and-do-it-right-34pk), [Collin Wilkins](https://collinwilkins.com/articles/structured-output), [aionda financial](https://aionda.blog/en/posts/json-schema-llm-financial-analysis-precision)

---

## 2. Chain-of-Thought + Temperature

**FinCoT** (arXiv 2506.16123): 금융 reasoning은 **temperature 0.2** 권장 (deterministic 0 아님). focused + consistent.

**Self-consistency**: temp>0으로 여러 reasoning chain 샘플링 → majority voting 또는 verifier.

**Faithful CoT**: symbolic reasoning + deterministic solver 결합.

출처: [FinCoT 논문](https://arxiv.org/html/2506.16123v1), [Faithful CoT](https://arxiv.org/html/2603.04663v1), [PromptHub CoT guide](https://www.prompthub.us/blog/chain-of-thought-prompting-guide)

---

## 3. Gemini Structured Output (2026)

- **모델 레벨 enforced** — guaranteed valid JSON matching schema
- **Semantic correctness는 보장 X** — code-level validation 필요
- 2026: 모든 Gemini 모델 JSON Schema 지원, Pydantic/Zod 호환
- tuned model + structured output = quality 감소 가능 (Flash는 untuned이므로 무관)

출처: [Google docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output), [Gemini API structured output](https://ai.google.dev/gemini-api/docs/structured-output), [oneuptime guide 2026](https://oneuptime.com/blog/post/2026-02-17-how-to-use-gemini-structured-output-and-json-mode-for-reliable-data-extraction/view), [Google blog JSON Schema](https://blog.google/technology/developers/gemini-api-structured-outputs/)

---

## 4. Hallucination Prevention (financial)

**7 Guardrails**:
1. Schema (structured output)
2. Retrieval grounding (RAG)
3. Tool use
4. **Verification** (supervisor)
5. Decoding constraints
6. **Policy gates**
7. Observability (telemetry)

**Multi-layered approach** — single technique X. RAG + CoT + RLHF + active detection + custom guardrails 결합.

**금융 특화**: gateway layer에 일관 enforcement (PII redaction / content safety / jailbreak detect / topic restriction / groundedness check).

출처: [Maxim fintech guardrails](https://www.getmaxim.ai/articles/llm-guardrails-for-fintech-compliance-hallucination-prevention-and-audit-trails/), [Nexumo 7 guardrails](https://medium.com/@Nexumo_/7-guardrails-that-reduce-llm-hallucinations-78facbb0d560), [Voiceflow 5 strategies](https://www.voiceflow.com/blog/prevent-llm-hallucinations), [Baytech finance](https://www.baytechconsulting.com/blog/hidden-dangers-of-ai-hallucinations-in-financial-services)

---

## 5. Few-Shot Prompting (trading)

- Few-shot = 변수 명명 / indent / comment 일관 + 모호 instruction → structured behavior
- 트레이딩: **명확한 entry 예시 + reject 예시 둘 다 필수**
- 스캘핑 패턴: ICT OTE, 9/21 EMA cross + rejection, double bottoms/tops, breakouts, S/R rejections

출처: [RogueQuant prompt eng for traders](https://roguequant.substack.com/p/prompt-engineering-for-traders-how), [LearnPrompting few-shot](https://learnprompting.org/docs/basics/few_shot), [TradeFundrr high-prob entry](https://tradefundrr.com/high-probability-scalp-entry/), [innercircletrader OTE](https://innercircletrader.net/tutorials/ict-optimal-trade-entry-ote-pattern/)

---

## 6. Adversarial / Self-Consistency / Confidence Calibration

**학술 2026** (매우 직접 관련):
- **TraderBench** (arXiv 2603.00285): AI 에이전트의 adversarial capital markets robustness
- **TrustTrade** (arXiv 2603.22567): Human-inspired selective consensus → 결정 uncertainty 감소
- **TradeTrap** (arXiv 2512.02261): LLM trading agent의 reliability/faithfulness 평가
- **Agentic confidence calibration** — adversarial 방법이 best calibration

**원칙**:
- Adversarial = calibration curve diagonal에 가깝게 만드는 *best 기법*
- Self-consistency = majority voting OR verifier-based confidence
- 트레이딩 특화: *consistent signals 우선, divergent discount, deterministic temporal anchor* + reflective memory → 노이즈/hallucination 억제

출처: [TraderBench](https://arxiv.org/html/2603.00285v1), [TrustTrade](https://arxiv.org/pdf/2603.22567), [TradeTrap](https://arxiv.org/pdf/2512.02261), [Agentic confidence calibration](https://www.emergentmind.com/topics/agentic-confidence-calibration), [CoT adversarial optimization](https://www.mdpi.com/2078-2489/16/12/1092)

---

## 정리 — METIS v4 prompt 디자인 원칙 (R4 초안)

| # | 원칙 | 근거 |
|---|---|---|
| **D1** | **Gemini native structured output** (response_schema) | 2026 production 표준. parsing 실패 0. |
| **D2** | **Temperature 0.1** (JSON 출력) — 0.0 너무 brittle, 0.2 reasoning용 | 컨센서스 + FinCoT |
| **D3** | **One schema per task** — Technical + Supervisor 같은 호출 내부 *섹션 분리* but 한 schema 출력 | structured output best practice |
| **D4** | **Reasoning first inside schema** — *internal reasoning fields 짧게*, 그 후 final decision | CoT 효과 + chain-of-thought 폭주 방지 |
| **D5** | **Few-shot: 4 ENTER + 4 NO_TRADE + 2 critical_reject** | 정상 + 거부 + 함정 패턴 학습 |
| **D6** | **Adversarial supervisor section** (same call): "If Technical says ENTER, what evidence opposes?" | adversarial = best calibration |
| **D7** | **Policy gate**: critical_reject_matched 비어 있지 않으면 ENTER 불가 (LLM이 직접 enforce + code 재검증) | Policy gates guardrail |
| **D8** | **No self-modification 명시 문구**: "Your job is to evaluate THIS cycle's data. You may not propose changes to rules, thresholds, or prompts." | reflective loop append-only 원칙 |
| **D9** | **Invalid JSON retry: 1회만**, 그 후 NO_TRADE force | latency/비용 제약 |
| **D10** | **Confidence calibration**: paper 누적 후 *bucket별 적중률* 추적. live에 prior 입력 X (룰 자기수정 금지) | TrustTrade |

---

## R4 GPT 회의 안건

### Q1: Temperature 최종 (0.0 / 0.1 / 0.2)
JSON 출력 + adversarial supervisor 같은 호출 = mixed needs. 어떤 값?

### Q2: Structured output 활용 깊이
Gemini native response_schema 사용. 그러나 *반올림된 numeric*, *enum constraint*, *array max length* 등 어디까지 schema에서 enforce?

### Q3: Reasoning fields in schema
"market_story" / "premortem" / "supervisor_check_reasoning" 같은 짧은 reasoning field를 schema에 *허용*할지. 허용 시 max char 제한?

### Q4: Setup taxonomy 5종 정의
R3 합의 5 setup 각각의:
- entry 조건 (구체)
- structural invalidation 가격 정의 방식
- critical reject 트리거 조건

### Q5: Few-shot example 구체 작성
4 + 4 + 2 = 10 examples면 prompt 토큰 큼. 비용 OK한지, 더 줄여야 하나? few-shot 없이 instruction-only가 Flash에서 작동하나?

### Q6: Adversarial supervisor 구현
Same call 안에서 "Technical says X. Supervisor finds opposing evidence Y." 패턴. 어떻게 구조화?

### Q7: Invalid JSON retry 전략
1회 retry 후 force NO_TRADE 합의. retry prompt 차이 (원본 + "previous response invalid, retry" 같은 minimal prefix)?

### Q8: Confidence calibration 운영
paper 누적 후 bucket별 적중률 — 어떻게 telemetry에서 보고만 하고 *live 룰 자기수정 X* 만드는지?

### Q9: Paper telemetry logging schema
trade_journal append-only. 어떤 필드 필수?

### Q10: 최종 prompt structure (system + user 부분 길이 / 섹션 순서)
1 call 안에 Technical + Supervisor + 5 setup taxonomy + 10 few-shot + 15 critical_reject + JSON schema = 토큰 폭주 위험. 압축 전략?
