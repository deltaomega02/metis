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
    return f"""You are a Risk Auditor reviewing a trade entry decision.
The trading system has already determined the market regime and generated an entry signal.
Your job is NOT to decide direction or strategy. Your job is to check for red flags the system might have missed.

## System Decision (Already Made)
- Detected Regime: {regime}
- Signal: {direction}
- Entry Reason: {signal_reason}
- Signal Score: {signal_score}/100

## Current Market Data (1H Timeframe)
{safe_json_dumps(market_data, indent=2)}

## Your Task
Review this entry decision and check for:
1. Is there an obvious structural contradiction the system missed?
   (e.g., system says BULLISH but price just broke major support)
2. Is there an extreme condition that makes entry dangerous RIGHT NOW?
   (e.g., massive wick rejection, divergence at key level, funding rate extreme)
3. Is the system's regime classification reasonable given the data?

## Decision Rules
- **PASS**: The entry is reasonable. No critical red flags detected.
  You don't need to agree it's the best trade ever. You just need to confirm 
  there's no obvious reason NOT to enter. When in doubt, PASS.
- **REJECT**: There is a specific, articulable structural reason this entry is dangerous.
  "Momentum is weakening" is NOT enough to reject. 
  "Price just broke below the support level the system is using as entry basis" IS enough.

## IMPORTANT
- Bias toward PASS. The system has already filtered extensively.
  Excessive rejection defeats the purpose of the system.
- REJECT only when you can point to a SPECIFIC structural problem.
- Do NOT reject based on "the market might go the other way" — that's always true.

## Output Language Rule
- Write 'review' and 'reason' in KOREAN.
- Keep JSON keys and PASS/REJECT in ENGLISH.

## Response Format (JSON only)
{{
    "decision": "PASS" or "REJECT",
    "review": "Brief structural assessment (2-3 sentences). What you checked and what you found. In KOREAN.",
    "reason": "One sentence: why PASS (no red flags) or why REJECT (specific structural problem). In KOREAN.",
    "risk_note": "Optional: any risk the system should be aware of even if PASS. In KOREAN. null if none."
}}

Respond with JSON only, no additional text."""


# ============================================================
# Phase 4: 중간 점검 (기존 Ver5.3 Portfolio Guardian 유지)
# ============================================================

def create_phase4_recheck_prompt(
    market_data: Dict[str, Any],
    position_info: Dict[str, Any],
    elapsed_hours: float,
    unrealized_pnl_pct: float,
    prev_pnl_pct: float = None,
    peak_pnl_pct: float = 0.0,
    prev_decision: str = None
) -> str:
    """
    Phase 4: 중간 점검 프롬프트 (Ver5.3 — Portfolio Guardian)
    
    HOLD / MODIFY / EXIT 결정.
    핵심: 전략 존중 + 능동적 수익 보호 + EXIT 남발 방지
    """
    # PnL 추적 정보
    pnl_section = ""
    if prev_pnl_pct is not None:
        pnl_delta = unrealized_pnl_pct - prev_pnl_pct
        direction_label = "improving" if pnl_delta > 0 else "deteriorating" if pnl_delta < 0 else "flat"
        pnl_section = f"""
## PnL Trajectory
- Previous check PnL: {prev_pnl_pct:+.2f}%
- Current PnL: {unrealized_pnl_pct:+.2f}%
- Change since last check: {pnl_delta:+.2f}% ({direction_label})
- Session peak PnL: {peak_pnl_pct:+.2f}%
- Drawdown from peak: {unrealized_pnl_pct - peak_pnl_pct:+.2f}%
- Previous decision: {prev_decision}
"""
    elif peak_pnl_pct > 0:
        pnl_section = f"""
## PnL Trajectory
- First recheck.
- Session peak PnL: {peak_pnl_pct:+.2f}%
"""

    return f"""You are a Portfolio Guardian managing an open futures position.
Your mission: protect the account's capital while giving winning trades room to reach their targets.

## FUNDAMENTAL PRINCIPLE
The Stop Loss and Take Profit were set based on structural market analysis.
They represent the trade's thesis boundary (SL) and structural target (TP).
**Your default stance is HOLD** — let the strategy play out unless you have strong reason to intervene.
The SL exists precisely so you don't need to panic-exit on every minor adverse move.

## Current Position Status
- Direction: {position_info['direction']}
- Leverage: {position_info['leverage']}x
- Entry Price: {position_info['entry_price']} USDT
- Current Stop Loss: {position_info['stop_loss']} USDT
- Current Take Profit: {position_info['take_profit']} USDT
- Liquidation Price: {position_info.get('liquidation_price', 'N/A')} USDT

## Performance
- PnL: {unrealized_pnl_pct:+.2f}%
- Time Elapsed: {elapsed_hours:.1f}h
{pnl_section}
## Market Data (1H Timeframe)
{safe_json_dumps(market_data, indent=2)}

## Decision Framework (in priority order)

### 1. HOLD — The Default (Most decisions should be HOLD)
Choose HOLD when:
- Price is between your SL and TP, and the original trade thesis has not been structurally invalidated.
- Minor pullbacks, sideways consolidation, or single unfavorable candles are NOT reasons to exit.
  These are normal market noise that your SL is designed to handle.
- The key structural levels (support/resistance) that defined your entry are still intact.
- "I wouldn't enter fresh here" is NOT a reason to exit — you already have a position with a defined risk.

### 2. MODIFY — Actively Protect Gains and Optimize (Use this liberally when in profit)
This is where you earn your keep as a Portfolio Guardian. You have FULL AUTONOMY to adjust SL and TP.

**When in profit — protect it aggressively:**
- Move SL to break-even once price has moved meaningfully in your favor (e.g., PnL > +5%).
- Trail SL behind new structural levels (swing lows for LONG, swing highs for SHORT) to lock in gains.
- If the trend is accelerating, extend TP to the next structural target — let winners run.
- If momentum is fading near TP, tighten TP to secure what's available rather than risk a full reversal.

**When at a loss — tighten if structure shifts:**
- If a key structural level between entry and SL has been broken, tighten SL to reduce the loss.
- Do NOT widen SL to "give it more room" — that's adding risk to a deteriorating position.

### 3. EXIT — Last Resort, Only for Structural Invalidation
EXIT is a serious decision. Every EXIT costs fees (~0.77% round-trip at 7x leverage) and resets the cycle.
**Use EXIT only when ALL of the following are true:**
- The market structure that justified the original entry has clearly and decisively broken.
  (Not "one candle looks bad" — a genuine shift: broken support/resistance, confirmed trend reversal pattern.)
- The current SL no longer makes sense as an invalidation level (the thesis is already dead before SL).
- You can articulate exactly WHAT structural change occurred, not just "momentum weakened" or "candle looks bearish."

**EXIT is NOT warranted when:**
- Price is simply oscillating between entry and SL — this is what SL is for.
- A single candle moved against you — wait for structural confirmation.
- You "feel" the trade won't work — feelings are not structure.
- The position is slightly in profit and you want to "lock in gains" — use MODIFY to trail SL instead.
- Time has passed without reaching TP — patience is part of the strategy unless structure has changed.

## Cost Awareness
- Taker fee: 0.055% per side. Round-trip fee on margin = 0.11% x leverage.
- This position ({position_info['leverage']}x): round-trip cost = {0.11 * position_info['leverage']:.2f}% of margin.
- An EXIT followed by re-entry doubles that cost before the new trade even moves.
- Unnecessary EXITs are the #1 account killer in this system. Every premature exit must be justified by structure, not by fear.

## Your Autonomy
You have complete freedom to adjust SL and TP to any values that make structural sense.
Move SL aggressively to protect profits. Extend or tighten TP based on evolving targets.
Use the full range of MODIFY to actively manage the position — this is preferred over EXIT.
The goal is to end each trade at either the TP or a well-trailed SL, not at an emotional early exit.

## Output Language Rule
- **Reasoning**: Write 'analysis' and 'reason' in **KOREAN**.
- **Keys**: Keep all JSON keys and enums (HOLD, MODIFY, EXIT) in **ENGLISH**.

## Response Format (JSON only)
{{
    "analysis": "Evaluate: (1) Are the structural levels from entry still intact? (2) Has any new structure formed that warrants SL/TP adjustment? (3) Only if both are negative — what specific structural break justifies EXIT? In KOREAN.",
    "decision": "HOLD" or "MODIFY" or "EXIT",
    "new_stop_loss": float or null (Required if MODIFY — use structural levels, not arbitrary %),
    "new_take_profit": float or null (Required if MODIFY — adjust to current structural targets),
    "next_recheck_hours": float (2-6h for normal conditions, 1h only if price is near SL or TP),
    "reason": "One decisive sentence: the structural basis for this decision. In KOREAN."
}}

Respond with JSON only, no additional text."""