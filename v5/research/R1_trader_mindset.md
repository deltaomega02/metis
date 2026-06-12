# R1 — 단타 트레이더 마인드 리서치 (1차 정리)

WebSearch 6건 (2026-05-28 기준) 결과 종합. 각 항목 출처 명시.

---

## 1. Linda Raschke — 단타 legend (Taylor Technique 기반)

- **Scalp = noise 큼**: 짧은 TF일수록 휩쏘. "back-and-fill" 시장.
- **트레일링 스탑 X, 한 번에 EXIT**: 짧은 TF에선 trailing이 노이즈에 잡힘 → 한 번에 청산.
- **진입 정밀도 최우선**: bid에 사고 ask에 팔기 50%+ 의식. 진입에서 edge 줄어드는 양 인식.
- **시장이 expected 대로 안 가면 *첫 reaction에서 EXIT***. 가격 타깃 X. 시장 액션이 말해줄 때 청산.
- **"Don't give back profits when short-term trading"**: 수익 돌려주지 마라.

출처: [studylib (Raschke rules)](https://studylib.net/doc/25927366/linda-raschke-rules-and-philosophy), [Robust Trader](https://therobusttrader.com/linda-raschke/), [Macro Ops](https://macro-ops.com/lessons-from-a-trading-great-linda-bradford-raschke/)

---

## 2. Tom Hougaard — "Best Loser Wins" (high-stake day trader, $30k → $1.3M in 1년)

- **"Best Loser Wins"**: *손실을 cleanly, quickly, consistently 받는* 트레이더가 살아남음.
- **시장이 아니라 자기 마음과의 싸움**: "사람들이 실패하는 이유는 차트 몰라서가 아니라 시장이 그들 *마음*에 하는 일을 몰라서다".
- **마인드 > 전략**: "전략 문제가 아니라 마인드 문제". 시스템에 *마인드 룰을 박는* 게 핵심.
- **100% 책임**: 시장/브로커/알고/뉴스 탓 안 함. (METIS의 "운영자 솔직 보고" 철학과 동일)

출처: [Tom Hougaard 책](https://www.amazon.com/Best-Loser-Wins-Thinking-high-stake/dp/085719822X), [Trade That Swing 정리](https://tradethatswing.com/best-loser-wins-how-tom-hougaards-mindset-can-transform-your-trading/), [Financial Wisdom TV](https://www.financialwisdomtv.com/post/the-best-loser-wins)

---

## 3. Mark Douglas — "Trading in the Zone" (확률적 마인드)

**Five Fundamental Truths**:
1. Anything can happen
2. 다음 결과를 알 필요 없이 돈을 벌 수 있다
3. Wins/losses는 *랜덤 분포* (한 거래 결과로 시스템 평가 X)
4. Edge = 한쪽 확률이 *약간* 더 높을 뿐
5. Every moment is unique (과적합 위험)

**Seven Principles**:
1. Edge 정의 (객관)
2. 리스크 사전 정의
3. 확률 사고 훈련
4. *정체성과 결과 분리*
5. 플랜대로 flawlessly 실행

**"The Zone"** = 결과에 detached, calm, focused. AI 시스템엔 *룰 정의가 곧 prompt 정의*.

출처: [Mind Math Money](https://www.mindmathmoney.com/articles/the-psychology-of-trading-why-traders-lee-money-mark-douglass-insights), [Trade That Swing](https://tradethatswing.com/key-takeaways-from-trading-in-the-zone-by-mark-douglas/), [Goodreads quotes](https://www.goodreads.com/work/quotes/245670-trading-in-the-zone-master-the-market-with-confidence-discipline-and-a)

---

## 4. 일반 프로 스캘퍼 룰 (2026 컨센서스)

- **거래당 리스크**: 자본의 0.5-1% (예: $1,000 시드 → $5-10/거래)
- **일일 손실 한도**: 자본의 1-2%. 도달 시 *그날 거래 중단*.
- **좁은 SL** (5-10 pip on majors) + **구조적 invalidation level**: 진입 *전*에 "여기면 thesis 틀렸다" 확정.
- **자동 SL 필수**: 감정 의사결정 제거. 진입 즉시 SL 셋업.

출처: [XS 15-min scalping](https://www.xs.com/en/blog/15-minute-scalping-strategy/), [Tradezella](https://www.tradezella.com/blog/scalping-strategies), [Trade with the Pros](https://tradewiththepros.com/tight-risk-scalping-plan/)

---

## 5. Crypto perpetual 단타 컨센서스

- 평균 holding **30-60분**, 0.1-5% profit/거래
- **TF <15분 추천 X** (노이즈). **30분 primary, 1h alternative** 권장
- Leverage **2-5x 충분** (50-200x 흥분용, 손실 가속)
- **51% 승률 + positive expectancy + 2% per-trade risk** → 손실 streak 견딤
- 표준 도구: EMA(추세), BB(변동성), RSI(모멘텀), ATR(SL), **VWAP(기관 level — METIS v3에 빠짐)**

출처: [CoinGape perpetual strategies](https://coingape.com/blog/crypto-perpetual-futures-trading-strategies/), [Blum perps strategies](https://www.blum.io/post/perps-strategies), [Highstrike guide](https://highstrike.com/perpetual-futures/)

---

## 6. Overtrading 심리 (METIS v3 손실 원인과 직결)

- **overtrading 원인**: 손실 회복 욕구 → 더 자주 진입 → 손실 가속 (= 운영자가 호소한 패턴)
- **방지**: hard daily trade limit (*opinion 아닌 data 기반*)
- FOMO/Fear/Greed = plan 이탈의 3 원인
- 단타는 *정신적으로 소모적* — 자동화의 본질적 가치 (감정 제거)

출처: [FasterCapital scalping psychology](https://fastercapital.com/content/Scalping-psychology--Mastering-the-Mindset-for-Quick-Profits.html), [Tradeify mistakes](https://tradeify.co/post/scalp-trading), [Opofinance risk tactics](https://blog.opofinance.com/en/riYOUR_OPENAI_API_KEY/)

---

## Claude 압축 — 7 원칙 (R1 초안, GPT 회의 전)

| # | 원칙 | 출처 | METIS 시사점 |
|---|---|---|---|
| **P1** | *확률적 마인드*: 단일 거래 결과로 시스템 평가 X. n=2/n=9 우상화 X | Douglas | 과적합 차단 (운영 정책 요구와 일치) |
| **P2** | *손실은 cleanly/quickly*: 자기 합리화로 손실 키우지 X | Hougaard | METIS Fix #43의 "리스크 인정하나 보완" 차단 패턴과 동일 — 강화 |
| **P3** | *진입 정밀도 > 청산 영리함* | Raschke | "어디 진입했는지가 90%" — setup 허용 기준 엄격화 (GPT가 지적한 진짜 손실 원인) |
| **P4** | *트레일링 스탑 신중*: 단타에선 한 번에 EXIT가 더 깔끔 | Raschke | METIS Fix #44 trailing (peak +0.6%, 0.3pp) 재평가 필요 |
| **P5** | *invalidation level 명시*: ATR×k 공식 X, *구조적 가격* | pro scalper consensus | METIS v3는 ATR 공식 — *구조 가격*으로 전환 검토 |
| **P6** | *자본 % 리스크 룰*: 거래당 0.5-1% / 일일 1-2% / hard limit | pro scalper consensus | METIS는 margin %만 봄 — *자본 %* 환산 + 일일 한도 도입 |
| **P7** | *15분 < 30분 권장*: 노이즈 한계. 30m primary 검토 | Crypto 단타 컨센서스 | METIS v3 15m primary → 30m primary 재고 |

**과적합 차단 원칙**: METIS 과거 거래는 *결론 근거*로 인용 X. 위 원칙은 *외부 트레이더 합의* 기반.
