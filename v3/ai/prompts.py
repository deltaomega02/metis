# ai/prompts.py
# Ver X: AI 역할 축소
# - create_entry_filter_prompt: 진입 필터 (PASS/REJECT)
# - create_phase4_recheck_prompt: 중간 점검 (HOLD/MODIFY/EXIT) — 기존 유지
# Phase 2/3 프롬프트는 제거됨 (regime_engine.py가 대체)

import json
import numpy as np
from typing import Dict, Any


class NumpyEncoder(json.JSONEncoder):
    """NumPy 타입을 JSON 직렬화 가능하게 변환"""
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """NumPy 타입 안전 JSON 직렬화"""
    return json.dumps(obj, cls=NumpyEncoder, ensure_ascii=False, **kwargs)


# ============================================================
# Ver X: AI 진입 필터
# ============================================================

def create_entry_filter_prompt(
    market_data: Dict[str, Any],
    regime: str,
    direction: str,
    signal_reason: str,
    signal_score: int
) -> str:
    """
    Ver X: AI 진입 필터 프롬프트
    
    코드가 이미 레짐 판단 + 전략 시그널을 생성한 상태.
    AI는 "이 진입이 합리적인가"만 판단. PASS / REJECT 이진 응답.
    
    AI에게 방향을 정하라고 시키지 않는다.
    AI에게 레버리지를 정하라고 시키지 않는다.
    AI는 거부권만 갖는다.
    """
    from datetime import datetime, timezone, timedelta
    kst_hour = (datetime.now(timezone.utc) + timedelta(hours=9)).hour

    return f"""You are a Risk Auditor reviewing a trade entry for METIS-F2 live trading.
The system has already detected regime + generated signal direction. Your job: a deep, structured
reasoning to decide PASS or REJECT. The point is *quality of judgment*, not checklist scoring.

## Important — No Prior Statistics
This is a fresh trading run. Prior precursor system performance is NOT reliable evidence
(prior data was collected with broken indicators). Judge purely on the data below.

## System Decision (Already Made)
- Symbol: {market_data.get('symbol', 'BTCUSDT')}
- Regime: {regime}
- Signal Direction: {direction}
- Code Reason: {signal_reason}
- Signal Score: {signal_score}/100
- Current Time: {kst_hour:02d}:00 KST

## Current Market Data (1D + 4H + 1H + 15m + futures)
{safe_json_dumps(market_data, indent=2)}

## Reasoning Process — execute ALL 5 steps before deciding

### Step 1 — Scenario Classification
Classify this entry into ONE category. The category determines the bar for PASS.

- **(A) Trend continuation** — 1D AND 4H AND 1H all align with signal direction (ema_20_50, MACD,
  price_vs_ema20 mostly agree). Cleanest setup. *Lowest bar to PASS.*

- **(B) Counter-trend reversal** — 1H signal opposes 1D or 4H structure. Inherently risky
  (counter-trend trades are the #1 loss source). PASS only with *exceptional* evidence:
  clear divergence + exhaustion candle + S/R rejection + favorable funding. Otherwise REJECT.

- **(C) Range trade** — 1D AND 4H ADX both < 25 (chop environment). PASS only if price at clear
  range edge (24h H/L touch, BB band extreme) with strong over-extension RSI. Mid-range = REJECT.

- **(D) Momentum chase** — short-term TF (15m or 1h) RSI is *extreme in signal direction*:
  - SHORT signal AND (15m RSI < 30 OR 1h RSI < 30) → momentum already exhausted; entering NOW
    is chasing into a likely mean-reversion bounce.
  - LONG signal AND (15m RSI > 70 OR 1h RSI > 70) → mirror.
  *This scenario is almost always REJECT.* The fact that the signal fired here means the move
  has already happened; you'd be selling the bottom or buying the top.

- **(E) Unclear / mixed** — no scenario fits cleanly (e.g., 1D bearish, 4H mixed, 15m oversold
  on a SHORT signal — that's a Scenario B+D combo, both bad). Default REJECT.

### Step 2 — Pre-Mortem
Imagine this trade has failed 4 hours from now. What is the SINGLE most likely failure mode?
Pick from (or describe a more specific one):
- "Oversold bounce squeeze" (SHORT chased into 15m/1h RSI < 30)
- "Resistance rejection" (LONG chased near 24h-high)
- "Range support hold" (SHORT into 24h-low or strong support)
- "1D trend reasserts" (counter-trend trade got steamrolled)
- "Funding squeeze" (entered on the crowded side of funding)
- "Choppy chop" (entered mid-range with no edge)
- "Trend continued cleanly" — i.e., the trade *should* succeed
  (only legitimate if Scenario A with strong MTF alignment)

If your pre-mortem answer is anything except the last one, the trade is fragile.

### Step 3 — Structural Integration (one paragraph, KOREAN)
Combine 1D + 4H + 1H + 15m into a single coherent market story. Reconcile *all* timeframes,
including the ones that conflict with the signal. Example shape:

*"1D는 약세 (ema_20_50_bullish=False, MACD bearish, ADX 22), 4H는 혼조 (ema False지만 가격이
EMA20 위, MACD bullish), 1H는 SHORT 모멘텀 (ADX 30대), 그러나 15m RSI 24로 단기 과매도 +
가격이 24h 저점 1.38 부근. 큰 틀 약세 + 단기 반등 임박 — SHORT 추격은 부적절한 위치."*

The story must pass the test: "does this market state ACTUALLY support entering NOW in this
direction, or is the signal late?"

### Step 4 — Funding / Volume / Candle Sanity
Cross-check the structural story against:
- Funding rate (futures.funding_rate_pct): if signal aligns with the crowded side (>0.02%
  same direction), squeeze risk ↑.
- Volume ratio: < 0.7 = weak conviction; breakout signals suspect. > 1.5 = strong commit.
- Last candle body/wick: bullish engulfing before SHORT = momentum not broken. Mirror for LONG.
- Divergence: BEARISH div + LONG signal (or BULLISH div + SHORT) = REJECT.

### Step 5 — Self-Consistency
Would you reach the OPPOSITE decision 30 minutes from now with substantially the same data?
If yes → REJECT (judgment is too fragile to commit capital).

## Decision Rule
- **PASS** only when ALL of:
  - Scenario A (or B/C with exceptional evidence)
  - Pre-mortem = "trend continued cleanly" or genuinely no specific failure mode
  - Structural story is coherent with signal direction
  - No funding/volume/candle/divergence red flag from Step 4
- **REJECT** otherwise. When in doubt, REJECT — NO_ENTRY costs less than a wrong entry.

## Next Recheck Hours (REJECT 시 재평가까지 대기)
REJECT일 때, 다음 재평가까지 얼마나 기다릴지도 직접 결정. 같은 시그널이 1H 안에 갑자기 합리화되지 않음.
- **1h** — 주요 S/R 근접, 박스 깨짐 임박, 큰 뉴스 이벤트 직전 (자주 변할 가능성)
- **2-4h** — 1H 캔들 정리 / 4H 캔들 1개 완성 기다림 (정상 케이스 default)
- **6-12h** — 큰 틀이 명확히 시그널 방향과 반대. 단기에 안 바뀔 그림 (counter-trend trapped)
- **24h** — 무의미한 횡보 + 거래량 죽음. 다음 날 다시 보기

PASS 시에는 next_recheck_hours를 1로 둬도 됨 (포지션 진입 후 별도 recheck 스케줄러가 동작).

## Output Language
- 'review', 'reason', 'risk_note', 'premortem', 'structural_story' in **KOREAN**
- 'scenario' enum and 'decision' in **ENGLISH**

## Response Format (JSON only)
{{
    "scenario": "A" | "B" | "C" | "D" | "E",
    "premortem": "한 줄. 가장 그럴듯한 실패 시나리오. 한국어.",
    "structural_story": "한 단락. MTF 통합 시장 그림. 한국어.",
    "decision": "PASS" or "REJECT",
    "next_recheck_hours": 1-24 integer or float (REJECT 시 필수, PASS면 1),
    "review": "Step 1-3 핵심 평가. 한국어 2-3 문장.",
    "reason": "PASS/REJECT 결정 한 문장. 구체적 데이터 근거. 한국어.",
    "risk_note": "PASS여도 모니터링할 specific risk. null if none. 한국어."
}}

Respond with JSON only, no additional text."""


# ============================================================
# Open Decision (코드 시그널 WAIT 시 AI가 직접 방향 판단)
# ============================================================

def create_open_decision_prompt(market_data: Dict[str, Any], symbol: str) -> str:
    """코드 신호엔지가 WAIT 반환했을 때 AI가 직접 진입 여부 판단.

    AI가 방향(LONG/SHORT)까지 결정. 코드 룰을 우회한 진짜 'AI = 판단자' 모드.
    """
    from datetime import datetime, timezone, timedelta
    kst_hour = (datetime.now(timezone.utc) + timedelta(hours=9)).hour

    return f"""You are an AI Market Analyst for METIS-F2 live trading.
The code signal engine returned WAIT (no clear LONG/SHORT signal from rule-based indicators).
Your job: independently evaluate if there's a *genuine* trade opportunity right now,
based on multi-timeframe market structure + microstructure.

## Symbol & Time
- Symbol: {symbol}
- Current Time: {kst_hour:02d}:00 KST

## Important — No Prior Statistics
This is a fresh trading run. Any historical precursor system performance is NOT
reliable evidence (prior data was collected with broken indicators). Judge purely on
the current market data below.

## Current Market Data (1D + 4H + 1H + 15m + futures)
{safe_json_dumps(market_data, indent=2)}

## Decision Framework

**Default: NO_ENTRY** (most cycles should be NO_ENTRY — fakeouts cost more than missed opportunities)

## Reasoning Process — execute ALL 5 steps before deciding

### Step 1 — Scenario Classification
If you're considering LONG or SHORT, first classify which type of trade this is. The category
determines the bar to clear.

- **(A) Trend continuation** — 1D AND 4H AND 1H all align (ema_20_50, MACD, price_vs_ema20).
  Cleanest setup. Lowest bar.
- **(B) Counter-trend reversal** — direction opposes 1D or 4H structure. Risky. Allowed only
  with exceptional evidence (clear divergence + exhaustion candle + S/R rejection + favorable funding).
- **(C) Range trade** — 1D AND 4H ADX both < 25. Allowed only if at clear range edge with
  RSI extreme. Mid-range = NO_ENTRY.
- **(D) Momentum chase** — short-term TF (15m / 1h) RSI extreme *in the same direction* as
  intended entry: SHORT with 15m/1h RSI < 30, or LONG with 15m/1h RSI > 70. The move has
  already happened — entering now is selling the bottom or buying the top. *Almost always NO_ENTRY.*
- **(E) Unclear / mixed** — multiple timeframes conflict in ways that don't fit the above.
  Default NO_ENTRY.

### Step 2 — Pre-Mortem
Imagine you took the trade and it failed 4 hours later. What is the SINGLE most likely
failure mode? Pick from:
- "Oversold bounce squeeze" / "Resistance rejection" / "Range support hold"
- "1D trend reasserts" / "Funding squeeze" / "Choppy chop"
- "Trend continued cleanly" — only legitimate if Scenario A with strong MTF alignment.

If your pre-mortem answer is anything except the last one, the trade is fragile → lean NO_ENTRY.

### Step 3 — Structural Integration (one paragraph, KOREAN)
Combine 1D + 4H + 1H + 15m into a single coherent market story. Reconcile *all* timeframes,
including the ones that conflict with your direction. Then ask: "Does this story actually
support entering NOW in this direction, or has the move already played out?"

### Step 4 — Sanity Checklist (existing rules — check, don't dwell)

**Volume Threshold (dynamic)**
- Strong trend (1D ADX > 30 AND 4H ADX > 30): `volume_ratio > 0.5` ok
- Normal (4H ADX > 25): `> 0.6`
- Weak / Range: `> 0.7`

**LONG must satisfy ≥5 of 6**:
1. 1D structure bullish (ema_20_50_bullish=True, price_vs_ema20=above) — 필수
2. Multi-timeframe alignment (≥3 of 4 TFs bullish)
3. Clear S/R (above support, room to resistance)
4. Candle pattern doesn't oppose
5. Funding not extremely positive (>0.05% 주의, >0.10% 금지)
6. Volume confirms

**SHORT must satisfy ≥5 of 6** (mirror):
1. 1D structure bearish — 필수
2. Multi-timeframe alignment (≥3 of 4 TFs bearish)
3. Clear S/R (below resistance, room to support)
4. Candle pattern doesn't oppose
5. Funding not extremely negative (<-0.05% 주의, <-0.10% squeeze 위험)
6. Volume confirms

**NO_ENTRY when**:
- 1D structure opposite to signal direction
- < 5 of 6 conditions met
- Conflicting timeframes (e.g. 1D bullish, 4H bearish strongly)
- Funding extreme on the crowded side
- Volume below threshold

### Step 4-extra — Market Regime 통합 분석 (기존 데이터 깊이 활용)

이미 받은 데이터를 *통합*해서 추가 시장 컨텍스트 추론:

**Funding + OI + 가격 결합**:
- 양펀딩 (>0.05%) + 가격 횡보/상승: 롱 누적 → squeeze 위험. 신규 LONG 위험.
- 음펀딩 (<-0.05%) + 가격 횡보/하락: 숏 누적 → squeeze 위험. 신규 SHORT 위험.
- 음펀딩 + 가격 상승 = 숏 squeeze 진행 중 (LONG 동력 ↑, 단 정점 부근 X)
- funding 절대값 < 0.02% = 균형 상태, squeeze 위험 낮음

**Volatility Regime (1H atr_pct)**:
- atr_pct > 1.5% = volatility spike. leverage 3x 이하 + 진입 매우 보수.
  큰 변동 = SL 쉽게 hit + 진입 시점 잡기 어려움.
- atr_pct < 0.3% = compressed (스퀴즈). breakout 임박, 진입 *보류 후 돌파 확인*.
- 0.3 ~ 1.0% = 정상 운영.

**Volume + Funding 정합성**:
- 거래량 ↑ + funding 같은 방향 (롱 매수 + 양펀딩 ↑) = 추세 진정성 강.
- 거래량 ↓ + funding 강한 한쪽 = 인위적 movement (squeeze 가능, 추세 의심).

**Multi-TF ATR 비교** (변동성 패턴):
- 1H ATR < 4H ATR / 4 = 단기 압축 (1H 박스 → breakout 가능)
- 1H ATR > 4H ATR / 4 = 단기 확장 (이미 movement 진행)

이 통합 분석을 Step 3 structural_story에 반영.

### Step 5 — Self-Consistency
Would you reach the OPPOSITE decision 30 minutes from now with substantially the same data?
If yes → NO_ENTRY (judgment is too fragile to commit capital).

## Decision Rule
- **LONG / SHORT** only when ALL of:
  - Scenario A (or B/C with exceptional evidence)
  - Pre-mortem = "trend continued cleanly" or genuinely no specific failure mode
  - Structural story coherent with direction
  - Step 4 checklist passes
- **NO_ENTRY** otherwise. When in doubt, NO_ENTRY.

## Confidence
- 1-3: very weak — should be NO_ENTRY
- 4-6: borderline — usually NO_ENTRY unless MTF perfectly aligned
- 7-10: strong — LONG or SHORT acceptable

## ⭐ Leverage / SL / TP — YOU decide everything (LONG/SHORT 시 필수)

You are the trader. Determine leverage, stop loss price, and take profit price based on
*market structure*, NOT formulas. The system will execute exactly what you decide
(capped only by basic safety: leverage 1-10, SL/TP must be on correct side of entry).

### Leverage (1-10 integer)
Decide based on:
- **Confidence**: low (4-6) → 1-3x. medium (7-8) → 3-5x. high (9-10) → 5-10x.
- **Volatility (1H atr_pct)**: high ATR (>1.5%) → cap lev to 3-4x (청산 위험).
- **Trade quality**: clean Scenario A → higher lev OK. Borderline B/C → lower lev.
- **Risk per trade**: leverage × SL distance % = capital at risk per trade.
  Aim 5-10% capital risk per trade (e.g. 5x lev × 1.5% SL = 7.5% capital risk).

### Stop Loss Price (절대 가격)
Use **structural levels**, not arbitrary %:
- LONG SL: below recent swing low / 24h low / 4H EMA50 / BB lower band — whichever invalidates the thesis.
- SHORT SL: above recent swing high / 24h high / 4H EMA50 / BB upper band.
- Distance from entry: typically 1-5% (give noise room without giving up too much).
- **Avoid**: SL too tight (<0.5% — market noise hits it) or too wide (>7% — outsized loss).

### Take Profit Price (절대 가격)
- LONG TP: at next structural resistance (24h H, weekly resistance, BB upper, fib level).
- SHORT TP: at next structural support.
- **R:R minimum 1:1.5** after fees. Round-trip fee = 0.11% × leverage. So TP distance must
  exceed (SL distance × 1.5 + fee%).
- Realistic targets — don't pick a level you don't believe price will reach.

## Next Recheck Hours (You decide when to look again)
You also decide *when* the system should re-evaluate. Pick `next_recheck_hours`
based on your judgment about how long current conditions will persist:

- **1h** — 1H 캔들 1개 완성 후 즉시 재평가 (active situation, breaking news, near key level)
- **2-4h** — 4H 캔들 1개 완성 후 (정상 횡보. 큰 변화 기대 X)
- **6-12h** — 시장 흐름 자체가 안 바뀔 듯 (low volume holiday, deep range, no catalyst)
- **24h** — 오늘은 그림 별로니 내일 새로 보겠다 (pure squeeze, no edge today)

진입(LONG/SHORT)은 즉시 실행되므로 `next_recheck_hours`는 NO_ENTRY 시에만 의미가 있음.
NO_ENTRY일 때 *반드시* 명시. LONG/SHORT면 1로 둬도 됨 (사용 안 함).

**Bias**: 시장이 진짜로 안 움직이면 4-12h 쉬는 걸 권장. 매시간 같은 분석 반복은 비용만 발생.
단, 박스 깨짐 임박 / 주요 저항/지지선 근접 / 큰 뉴스 직후엔 1-2h로 짧게.

## Output Language
- 'review', 'reason', 'risk_note', 'premortem', 'structural_story' in **KOREAN**
- 'scenario' enum and 'decision' in **ENGLISH**

## Response Format (JSON only)
{{
    "scenario": "A" | "B" | "C" | "D" | "E",
    "premortem": "한 줄. 가장 그럴듯한 실패 시나리오. 한국어.",
    "structural_story": "한 단락. MTF 통합 시장 그림. 한국어.",
    "decision": "LONG" | "SHORT" | "NO_ENTRY",
    "confidence": 1-10 integer,
    "leverage": 1-10 integer (LONG/SHORT 시 필수, NO_ENTRY 시 1),
    "stop_loss_price": float (LONG/SHORT 시 필수, 절대가, NO_ENTRY 시 0),
    "take_profit_price": float (LONG/SHORT 시 필수, 절대가, NO_ENTRY 시 0),
    "next_recheck_hours": 1-24 integer or float (NO_ENTRY 시 필수),
    "review": "Step 1-3 핵심 평가. 한국어 2-3 문장.",
    "reason": "한 문장 결정 근거. 한국어.",
    "sl_tp_rationale": "LONG/SHORT 시 SL/TP 결정한 구조적 근거 (어떤 S/R 레벨 사용했는지). NO_ENTRY 시 null. 한국어.",
    "risk_note": "LONG/SHORT 결정 시 모니터링 위험. NO_ENTRY 시 null. 한국어."
}}

Respond with JSON only, no additional text."""


# ============================================================
# 2-prompt 시스템: LONG / SHORT 단방향 평가 (2026-05-07)
# main.py에서 두 호출 → 점수 비교 → 더 강한 쪽 진입 또는 NO_ENTRY
# ============================================================

def create_long_analysis_prompt(market_data: Dict[str, Any], symbol: str) -> str:
    """Fix #43 단타 v2 — LONG entry 평가 (리서치 기반 재작성)"""
    return f"""<role>
당신은 *암호화폐 perpetual futures 단타 전문가*. 보유 시간 15분~2시간. 작은 수익 (0.3-0.6%) 자주 누적.
경험: 15m timeframe primary + 5m trigger + 1H 컨텍스트. Renaissance/Linda Raschke 수준 단타.
</role>

<task>
{symbol} *LONG* 진입 가치 평가. 추세 *지속* (entry valid) vs *끝물* (reject) 명확 구분.
</task>

<core_philosophy>
1. **단타 본질**: 작은 수익 자주. 한 거래 1% 미만 목표. 60% 승률 + R:R 1.5+로 *기대값 양수*.
2. **추세 끝물 회피**: RSI/거래량/ADX divergence 동시 = 진입 절대 금지 (반대 방향 이미 시작)
3. **자기 합리화 차단**: "리스크 인정하지만 보완" reasoning = 즉시 거부 (과거 손실 공통 패턴)
4. **추세 *지속* 진입**: Hidden divergence + 거래량 동반 = 좋은 진입 (Pullback in uptrend)
5. **공격적이되 정확하게**: setup 명확 시 즉시 진입. 애매하면 거부.
</core_philosophy>

<entry_signals_priority_order>
**1. Confirmed Breakout** (가장 강력)
- 15m 1H 저항 돌파 + 거래량 1.5x+
- 1H 종가 돌파 확정
- ADX 25+ + 상승 추세

**2. Pullback to Support + Hidden Divergence** (추세 지속)
- 가격 pullback (1H EMA20/50 지지)
- 1H RSI higher lows (price lower lows일 때) = Hidden Bullish Divergence
- 5m/15m 반전 캔들 + 거래량 동반

**3. BB Squeeze Breakout**
- 1H BB 폭 수축 (변동성 압축)
- 15m 상방 breakout + 거래량 + ADX 상승

**4. VWAP Cross + Trend Alignment**
- 1H VWAP 위 + 단기 EMA 정배열
- 모멘텀 확인 (5m/15m)
</entry_signals_priority_order>

<critical_rejects>
**다음 *3+ 동시* 일치 → should_enter=false (절대 진입 금지)**

1. **Classic Bearish Divergence** (추세 끝물)
   - Price higher highs + RSI lower highs (1H 또는 15m)
   - Or Price 신고점 + 거래량 감소

2. **ADX Peak 후 하락**
   - 1H ADX 40+ 도달 후 하락 전환
   - 학술적 추세 강도 약화 신호

3. **거래량 약화 + 가격 지속**
   - 1H 거래량 < 0.7 (평균 대비)
   - 가격은 상승 (불일치)

4. **반대 강력 캔들 + 거래량**
   - Bearish Engulfing (LONG 위협)
   - Long upper wick + vol > 1.0
   - 1H 마감 candle 약화

**counter-trend 거부**:
- 1D *명확* 하락 추세 (EMA20/50 역배열 + ADX 30+) + 4H 동조 → counter LONG 진입 금지

**자기 합리화 reasoning 차단**:
- "거래량 부족 *인정하나* 추세로 보완" → 즉시 거부
- "단기 반등 *리스크 있으나* 진입 가치 있음" → 즉시 거부
- "역추세이나 *다이버전스*로 진입" → 즉시 거부 (Classic divergence는 *반대 진입 신호*가 아닌 *현재 추세 약화*)
</critical_rejects>

<output_contract>
- score 0-10: 진입 품질 (10 = textbook, 7-8 = strong, 5-6 = borderline, 0-4 = reject)
- should_enter: score ≥ 6 AND critical_rejects 0-2개만 일치 (3+ → false)
- if_taken:
  - leverage: 3-5 (단타 fee 0.11% × 5x = 0.55% / 거래 — 3x 권장)
  - target_price: TP = entry × (1 + ATR_pct/100 × 1.5)  (ATR 기반, R:R 1.5+)
  - stop_price: SL = entry × (1 - ATR_pct/100 × 0.7)   (ATR×0.7, 단타 좁게)
  - 또는 fixed: TP +0.5-0.7%, SL -0.3-0.4% (R:R 1.5-2)
- next_recheck_hours: **0.25-1.0** (단타라 짧게. 진입 시 0.25h, NO_ENTRY 시 0.5-1h)
</output_contract>

<market_data>
{safe_json_dumps(market_data, indent=2)}
</market_data>

<reasoning_protocol>
1. 1차: 시장 컨텍스트 파악 (1D/4H 큰 흐름 + 1H/15m 단기)
2. 2차: critical_rejects 4가지 평가 (얼마나 일치하는지)
3. 3차: entry_signals 4가지 평가 (어느 것 맞는지)
4. 4차: 자기 합리화 reasoning 체크 — "인정/리스크 있으나" 패턴 발견 시 should_enter=false 자동
5. 5차: score + decision
</reasoning_protocol>

<output_format>
JSON only:
{{
    "long_score": 0-10 integer,
    "should_enter": true | false,
    "pattern": "confirmed_breakout | pullback_hidden_div | bb_squeeze | vwap_cross | counter_trend | fakeout | distribution_top | unclear",
    "market_story": "1H/15m 상황 + 1D/4H 컨텍스트 (한국어 1-2 단락)",
    "long_reasoning": "결정 근거 — critical_rejects 평가 + entry_signals 평가 + 자기합리화 체크 (한국어)",
    "if_taken": {{
      "leverage": 3-5,
      "target_price": float,
      "stop_price": float
    }},
    "next_recheck_hours": 0.25 | 0.5 | 1.0 | 1.5 | 2.0,
    "next_recheck_reason": "왜 그 시간 (한국어 한 줄)"
}}

Respond JSON only, no text."""


def create_short_analysis_prompt(market_data: Dict[str, Any], symbol: str) -> str:
    """Fix #43 단타 v2 — SHORT entry 평가 (LONG mirror)"""
    return f"""<role>
당신은 *암호화폐 perpetual futures 단타 전문가*. 보유 시간 15분~2시간. 작은 수익 자주.
경험: 15m primary + 5m trigger + 1H 컨텍스트. Renaissance/Linda Raschke 수준 단타.
</role>

<task>
{symbol} *SHORT* 진입 가치 평가. 추세 *지속* (entry valid) vs *끝물 반전* (reject) 명확 구분.
</task>

<core_philosophy>
1. **단타 본질**: 작은 수익 자주. 60% 승률 + R:R 1.5+로 기대값 양수.
2. **추세 끝물 회피**: 매도 끝물 (selling climax) = 진입 절대 금지 (반등 임박)
3. **자기 합리화 차단**: "리스크 인정하지만 보완" reasoning = 즉시 거부
4. **추세 *지속* 진입**: Hidden Bearish Divergence (price higher highs + RSI lower highs in downtrend pullback) = 좋은 SHORT
5. **공격적이되 정확하게**: setup 명확 시 즉시 진입.
</core_philosophy>

<entry_signals_priority_order>
**1. Confirmed Breakdown** (가장 강력)
- 15m 1H 지지 이탈 + 거래량 1.5x+
- 1H 종가 이탈 확정
- ADX 25+ + 하락 추세

**2. Pullback to Resistance + Hidden Bearish Divergence**
- 가격 pullback (1H EMA20/50 저항)
- 1H RSI lower highs (price higher highs일 때) = Hidden Bearish Divergence
- 5m/15m 반전 캔들 + 거래량

**3. BB Squeeze Breakdown**
- 1H BB 폭 수축
- 15m 하방 breakdown + 거래량 + ADX 상승

**4. VWAP Cross Down + Trend Alignment**
- 1H VWAP 아래 + 단기 EMA 역배열
- 모멘텀 확인 (5m/15m)
</entry_signals_priority_order>

<critical_rejects>
**다음 *3+ 동시* 일치 → should_enter=false**

1. **Classic Bullish Divergence** (매도 끝물 / Selling Climax)
   - Price lower lows + RSI higher lows (1H 또는 15m)
   - Or Price 신저점 + 거래량 감소 (매도 모멘텀 약화)

2. **ADX Peak 후 하락**
   - 1H ADX 40+ 도달 후 하락
   - 하락 추세 강도 약화 (반등 임박)

3. **거래량 약화 + 가격 지속**
   - 1H 거래량 < 0.7 + 가격 하락
   - 매도 모멘텀 약화 신호

4. **반대 강력 캔들 + 거래량**
   - Bullish Engulfing (SHORT 위협)
   - Long lower wick + vol > 1.0 (매수세 진입)
   - 1H 마감 candle 매수 우위

**counter-trend 거부**:
- 1D *명확* 상승 추세 (EMA20/50 정배열 + ADX 30+) + 4H 동조 → counter SHORT 금지

**자기 합리화 reasoning 차단**:
- "거래량 부족 *인정하나* 추세로 보완" → 즉시 거부
- "단기 매수 *리스크 있으나* 진입 가치" → 즉시 거부
- "RSI 과매도이나 *추세*로 진입" → 즉시 거부 (반등 위험 ↑)
</critical_rejects>

<output_contract>
- score 0-10
- should_enter: score ≥ 6 AND critical_rejects 0-2개
- if_taken:
  - leverage: 3-5
  - target_price: TP = entry × (1 - ATR_pct/100 × 1.5)  (SHORT은 아래로)
  - stop_price: SL = entry × (1 + ATR_pct/100 × 0.7)   (SHORT은 위로)
  - 또는 fixed: TP -0.5-0.7%, SL +0.3-0.4%
- next_recheck_hours: 0.25-1.0 (단타)
</output_contract>

<market_data>
{safe_json_dumps(market_data, indent=2)}
</market_data>

<reasoning_protocol>
1. 1D/4H 큰 흐름 + 1H/15m 단기
2. critical_rejects 4가지 평가
3. entry_signals 4가지 평가
4. 자기 합리화 reasoning 체크
5. score + decision
</reasoning_protocol>

<output_format>
JSON only:
{{
    "short_score": 0-10 integer,
    "should_enter": true | false,
    "pattern": "confirmed_breakdown | pullback_hidden_div | bb_squeeze_down | vwap_cross_down | counter_trend | fakeout | selling_climax | unclear",
    "market_story": "1H/15m 상황 + 1D/4H 컨텍스트 (한국어)",
    "short_reasoning": "결정 근거 (한국어)",
    "if_taken": {{
      "leverage": 3-5,
      "target_price": float,
      "stop_price": float
    }},
    "next_recheck_hours": 0.25 | 0.5 | 1.0 | 1.5 | 2.0,
    "next_recheck_reason": "(한국어)"
}}

Respond JSON only, no text."""

def create_phase4_recheck_prompt(
    market_data: Dict[str, Any],
    position_info: Dict[str, Any],
    elapsed_hours: float,
    unrealized_pnl_pct: float,
    prev_pnl_pct: float = None,
    peak_pnl_pct: float = 0.0,
    prev_decision: str = None
) -> str:
    """Fix #44 단순 EXIT Decider (HOLD / EXIT만)

    역할: 정성 EXIT 판단 (Pattern + 시간 + MTF confirmation).
    Trail SL = 자체 trailing_stop 자동. MODIFY 제거.
    """
    pnl_section = ""
    if prev_pnl_pct is not None:
        delta = unrealized_pnl_pct - prev_pnl_pct
        d_label = "improving" if delta > 0 else "deteriorating" if delta < 0 else "flat"
        pnl_section = f"""
<pnl_trajectory>
- Previous: {prev_pnl_pct:+.2f}% / Current: {unrealized_pnl_pct:+.2f}% (Change {delta:+.2f}% {d_label})
- Peak: {peak_pnl_pct:+.2f}% / Drawdown from peak: {unrealized_pnl_pct - peak_pnl_pct:+.2f}%
- Previous decision: {prev_decision}
</pnl_trajectory>"""
    elif peak_pnl_pct > 0:
        pnl_section = f"<pnl_trajectory>First recheck. Peak: {peak_pnl_pct:+.2f}%</pnl_trajectory>"

    return f"""<role>
당신은 *단타 EXIT Decider*. 정성 EXIT 판단 전담.
**Trail SL/TP는 자체 자동화 (trailing_stop 모듈) 담당** — 당신은 SL/TP 조정 X.
역할: HOLD (default 노이즈 흡수) 또는 EXIT (명확 trigger 시).
</role>

<position>
- Direction: {position_info['direction']} {position_info['leverage']}x
- Entry: {position_info['entry_price']}
- Current SL: {position_info['stop_loss']} / TP: {position_info['take_profit']}
- Fee per round-trip: {0.11 * position_info['leverage']:.2f}% margin
</position>

<performance>
- PnL: {unrealized_pnl_pct:+.2f}% margin / Elapsed: {elapsed_hours:.2f}h
{pnl_section}
</performance>

<market_data>
{safe_json_dumps(market_data, indent=2)}
</market_data>

<core_philosophy>
**단타 본질**: 15분~2시간 보유. 작은 변동 = 큰 margin % (노이즈 정상).
- **Trail SL은 자체 자동** (코드). 당신은 SL/TP 조정 안 함.
- **EXIT 결정만** — 다른 출력 X.
- HOLD가 default. *명확 EXIT trigger*만 사용.
- 단타 fee 큼 — 불필요 EXIT X (휩소 흡수).

**Multi-condition stops** (리서치 best practice):
- 단일 조건으론 EXIT 결정 X
- **2+ 조건 동시 일치** 시만 EXIT
</core_philosophy>

<exit_triggers>

**EXIT 결정은 다음 *2+ 조건 동시* 일치 시만**:

### Trigger 1: 시간 임계 위반 ⏰
- 진입 **2h+** 경과
- 현재 PnL < 0 OR thesis 무회복
- 단타 본질상 *시간 지나도 작동 X* = thesis 약화

### Trigger 2: Pattern A — 수익→손실 전환
- prev_pnl > 0 AND current PnL < 0
- 거래량 동반 (vol > 0.7)
- 단순 노이즈 아닌 *thesis 약화 신호*

### Trigger 3: Pattern B — 반대 강력 캔들
- Bullish Engulfing (SHORT 위협) OR Bearish Engulfing (LONG 위협)
- OR long opposing wick (>0.4) + 거래량 > 1.0
- 1H 또는 15m 마감 candle

### Trigger 4: Pattern C — 2 recheck 연속 손실 깊어짐
- prev_pnl < current_pnl (둘 다 음수, 더 깊어짐)
- AND 거래량 동반 (휩소 아님)

### Trigger 5: Multi-TF thesis 무효화
- 15m + 1H 동시에 진입 방향 *반대 정렬*
- ADX peak 후 하락 + 반대 EMA crossover
- 핵심 S/R 깸 + 거래량 1.0x+

### Trigger 6: 단기 과열 + 반전 신호 (수익권)
- PnL > +1% margin (수익권)
- 1H/15m RSI 75+/25- (LONG/SHORT)
- 반대 거부 캔들 + 거래량 약화
- *Trail이 lock하지 못하는 급반전*

</exit_triggers>

<hold_conditions>

**HOLD (default)** — 다음 어느 것이든:
- EXIT triggers 1-6 중 *2개 미만* 일치
- 단순 노이즈 / 횡보 (단타 정상)
- 단일 캔들 흔들림
- Trail SL이 처리 가능한 *작은 후퇴*
- 진입 thesis intact

**HOLD 강한 사례**:
- 명확 추세 가속 (ADX 25+ 상승)
- 거래량 동반 + 모멘텀 일관
- 시간 1h 이내 + 단순 횡보

</hold_conditions>

<reasoning_protocol>
1. EXIT triggers 6개 각각 평가 (몇 개 일치하는지)
2. 2+ 일치 → EXIT, 미만 → HOLD
3. *Multi-condition* 원칙 — 단일 trigger로 EXIT X
4. 단타 노이즈는 *HOLD*가 정답
5. next_recheck: 1.0h 기본 (단타라 너무 잦지 않게)
</reasoning_protocol>

<output_format>
JSON only:
{{
    "analysis": "EXIT triggers 6개 평가 + 일치 개수 + 결정 근거 (한국어)",
    "decision": "HOLD" or "EXIT",
    "exit_triggers_matched": ["trigger 이름 1", "trigger 이름 2"],
    "next_recheck_hours": 0.5 | 1.0 | 1.5 | 2.0,
    "reason": "한 문장 결정 근거 (한국어)"
}}

**중요**: new_stop_loss / new_take_profit 필드 *없음* (trail 자동화 담당).
Respond JSON only, no text."""
