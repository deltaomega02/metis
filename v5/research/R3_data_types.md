# R3 — 단타 effective 데이터 리서치 (1차 정리)

WebSearch 6건. 2026-05-28.

---

## 1. CVD (Cumulative Volume Delta)

- aggressive 참여 vs thin liquidity 구분
- **Divergence**: price 신고점 + delta 미동조 = 약화/반전 신호
- 스캘퍼: 실시간 CVD spike → 단기 LONG/SHORT setup
- *Flash 한계*: raw tick 입력 X. **aggregate metric** (CVD slope / CVD divergence flag) 만 입력 가능

출처: [Bookmap CVD](https://bookmap.com/blog/how-cumulative-volume-delta-transform-your-trading-strategy), [Phemex CVD guide](https://phemex.com/academy/what-is-cumulative-delta-cvd-indicator), [coinperps](https://www.coinperps.com/learn/what-is-cumulative-volume-delta-cvd)

---

## 2. Order Book Imbalance (OBI)

- bid/ask 비율, -1 ~ +1 (보통 ±0.5 진동)
- Bybit Level 1000 orderbook **200ms push** (2026)
- *Flash 한계*: 200ms push = 호출 cycle (분 단위)과 불일치. **요약 metric** (OBI 5초/30초 평균, 임계 돌파 flag) 만 활용

출처: [LiteFinance order flow](https://www.litefinance.org/blog/for-beginners/trading-strategies/order-flow-trading-with-footprint-charts/), [Amberdata heatmap](https://blog.amberdata.io/leveraging-order-book-heatmaps-and-trade-order-flow-for-market-trend-analysis), [Buildix 2026](https://www.buildix.trade/blog/free-crypto-orderflow-tools-guide-2026)

---

## 3. Footprint Charts

- 캔들 내부 buy/sell imbalance per price
- 단타 prone — 단, **LLM 입력엔 너무 raw**. *aggregate flag* 추출만.

출처: [Day Trading Profit Calculator](https://www.daytradingprofitcalculator.com/blog/order-flow-trading-explained), [LiteFinance footprint](https://www.litefinance.org/blog/for-beginners/trading-strategies/order-flow-trading-with-footprint-charts/)

---

## 4. Liquidation Heatmap (CoinGlass / Kalena 등)

- 큰 cluster = stop-hunt magnet
- **양 펀딩 + 가격 아래 cluster** = 단기 reversal 고확률 setup
- **12h/24h heatmap = 단타**, 7-30일 = 스윙
- 2026-02-23 사례: $61.5M long liquidation → 137,422명 청산 cascade. $468M/day total.
- Coinglass, Cryptopolitan, Kalena 등에서 API 제공

출처: [Zipmex 2026 guide](https://zipmex.com/blog/what-is-a-liquidation-heatmap/), [Kalena heatmap 5 strategies](https://blog.kalena.ai/liquidation-heatmap-where-leveraged-positions-go-to-die-and-how-smart-traders-get-there-first), [CoinGlass](https://www.coinglass.com/pro/futures/LiquidationHeatMap), [Bitget 2026](https://www.bitget.com/academy/12560603880696)

---

## 5. Open Interest + Funding Rate Divergence

- **OI 증가 + funding 양** → 1-2일 내 breakout 시도 (방향 미정)
- **funding + price 불일치** = squeeze 위험 신호
- 전문가는 funding *단독* X. **OI + price + volume + liquidation** 통합.
- Higher leverage + higher funding + lower price = fragile setup

출처: [Lambda Finance funding 2026](https://www.lambdafin.com/articles/crypto-funding-rates-april-2026), [Gate web3 OI+funding 2026](https://web3.gate.com/crypto-wiki/article/how-do-futures-open-interest-and-funding-rates-signal-crypto-derivatives-market-trends-in-2026-20260202), [Zipmex funding 2026](https://zipmex.com/blog/how-to-analyze-funding-rates-in-crypto/)

---

## 6. Anchored VWAP

- session anchor / first high-vol candle anchor / breakout level anchor
- 추세 중 VWAP **지지/저항** 작동
- *Multiple AVWAP* (서로 다른 anchor) 동시 참조 — 기관 reading
- 스캘퍼는 session AVWAP + 직전 큰 캔들 AVWAP 2개 정도

출처: [Alphatrends anchored VWAP](https://alphatrends.net/anchored-vwap/), [TrendSpider AVWAP](https://trendspider.com/learning-center/anchored-vwap-trading-strategies/), [Chart Champions](https://blog.chartchampions.com/vwap-and-anchored-vwap/)

---

## 7. SOL 특성 (2026 기준)

- 30일 realized volatility **60-90% annualized** (BTC 40-60% 대비 1.5-2배)
- ATR 큼 → 단타 가격 변동 충분 (단, 좁은 SL은 노이즈 risk ↑)
- funding 보통 0.01-0.05% (8h)
- daily volume $2B+ (유동성 충분)

출처: [Bitget SOL price](https://www.bitget.com/academy/solana-price-data), [Bitrue SOL perpetual](https://www.bitrue.com/blog/how-to-trade-solana-perpetual-futures-for-profit), [Glassnode SOL funding](https://studio.glassnode.com/charts/derivatives.FuturesFundingRatePerpetual?a=SOL), [Bitget Solana futures guide 2026](https://www.bitget.com/academy/solana-futures-guide)

---

## 8. Bybit V5 API 사용 가능 데이터

- Orderbook Level 1000: **200ms push**
- Funding history: `/v5/market/funding/history`
- Liquidation, Position, Order, Wallet
- Kline, Ticker, Trade
- 추가 (Jan 2026): DataTimeLocal, DataAge property

출처: [Bybit V5 changelog](https://bybit-exchange.github.io/docs/changelog/v5), [funding history endpoint](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)

---

## 데이터 후보 매트릭스 (R3 초안)

| Tier | 데이터 | 용도 | Flash 가능? | 비용 |
|---|---|---|---|---|
| **T0 필수 (OHLCV + 표준 지표)** | 15m/1h/4h kline, EMA/RSI/MACD/ADX/ATR/BB | regime + entry trigger | aggregate | Bybit 무료 |
| **T1 강력 (perpetual 특화)** | funding rate (8h history), open interest, liquidation 24h volume, 1H taker buy/sell ratio | 레짐 + squeeze 위험 | aggregate | Bybit 무료 |
| **T2 도움 (구조 가격)** | 24h H/L, 3d H/L, recent swing H/L, session AVWAP | invalidation level + structural reference | pre-computed | Bybit + 자체 계산 |
| **T3 부가** (선택) | OBI 5초/30초 평균, CVD slope, liquidation heatmap top 3 cluster | 진입 timing | 200ms→cycle aggregate | Bybit + Coinglass API |
| **T4 외부** | BTC/ETH 같은 TF kline (beta context), macro event marker (CPI/FOMC/KST 09) | regime filter | aggregate | Bybit + 외부 calendar |
| **금지** | raw tick, raw orderbook depth, raw footprint, raw news/sentiment | Flash 무력 + 비용 폭증 | | — |

---

## R3 GPT 회의 안건

### Q1: 최소 feature schema
T0 + T1 + T2 + T4(BTC context)로 출발하는 게 합리적? T3는 *backtest 검증 후 추가*가 맞나?

### Q2: 입력 빈도
- 분석 cycle = 15분/30분/1h 중 어느 것?
- 200ms orderbook push = LLM 입력 X. 그러나 *임계 돌파 flag*는 stream worker로 따로 수집해서 cycle에 합치는 게 효과적?
- v3 = 1H 주기였음. v4 권장 cycle은?

### Q3: BTC/ETH context
SOL 단독 거래라도 BTC가 크게 움직이면 SOL 동조. 어느 정도 BTC/ETH 데이터를 *몇 줄*로 feature schema에 박을지.

### Q4: Macro/event marker
- 데이터 source: TradingView economic calendar / Forex Factory / 자체 hardcode?
- 자동 fetch 어렵다면 *주 1회 manual 업데이트* 허용?

### Q5: Walk-forward 데이터 분할 (R2 합의 보강)
SOL 15m/30m/1h 기준 *얼마만큼 historical* 필요?
- 60-90일 calibration + 14-30일 validation + 14-30일 OOS = 합의됨
- 그런데 SOL 단타 데이터는 *몇 개 거래 표본*이 되야 의미 있나? (Q4 시간 표본 → 거래 표본 환산)

### Q6: Final JSON contract
Gemini Flash 출력 schema를 R3에서 미리 확정해서 R4 prompt design 들어갈 때 *블록* 명확하게 — 어떤 필드가 필수인가?
