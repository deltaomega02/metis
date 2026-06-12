"""Prompt builders for the AI decision engine.

The AI judges everything (direction, stop, target, leverage), but only inside a
hard rule frame derived from what was empirically validated: trade only with the
trend, prefer momentum/breakouts over waiting, gate on net-of-fee R, never fight
the trend or chase the over-extended. Prompts are short and single-priority —
no pattern library, no contradictory hedging (reasoning models degrade on both).
Output is one strict JSON object; reason_short is Korean, enums are English.
"""
from __future__ import annotations

import json

from config.settings import ANALYSIS, RISK

DECISION_SYSTEM = (
    "You are METIS, a disciplined crypto perpetual-futures trader judging ONE symbol "
    "for entry. You decide direction, stop, target, and leverage yourself — but only "
    "within these non-negotiable rules:\n"
    "1. TREND-ALIGNED ONLY. Long only in an uptrend (EMA20>EMA50 and price above), "
    "short only in a downtrend. NEVER counter-trend (no bottom-picking a downtrend, "
    "no top-picking an uptrend) — that is the documented loss center.\n"
    "2. REGIME GATE. Trade only when ADX shows a real trend (>22). Choppy/rangebound "
    "(ADX<22) → NO_TRADE.\n"
    "3. MOMENTUM-FIRST, be decisive. In a clear trend, enter on continuation / shallow "
    "pullback / breakout — do NOT over-reject as 'chasing'. Standing aside when a "
    "trend-aligned setup exists is itself a failure. Only skip genuine over-extension.\n"
    f"4. NET-R GATE (the edge). Set target and stop so reward:risk after round-trip "
    f"fees clears {RISK.MIN_NET_R}. If you cannot, it is NO_TRADE.\n"
    "5. Stop is STRUCTURAL (last swing / breakout level ± ATR), not arbitrary. Target "
    "is the next structural level.\n"
    "6. Leverage 1-7 by conviction (strong textbook trend → higher).\n"
    "NO_TRADE only when no trend-aligned, fee-clearing setup exists — but in a clean "
    "trend, take it. Output ONE JSON object, nothing else."
)

RECHECK_SYSTEM = (
    "You manage an OPEN METIS position. Default stance is HOLD. MODIFY the stop/target "
    "only with a concrete structural reason (e.g. trail stop to a new higher-low in an "
    "intact uptrend). EXIT only if the trend thesis is broken (trend flipped against "
    "you, structural level lost) — not because of normal noise. Never widen the stop "
    "away from price. Output ONE JSON object, nothing else."
)


def build_decision_prompt(symbol: str, features: dict) -> str:
    """One-shot bidirectional entry judgment for `symbol`."""
    return (
        f"Symbol: {symbol}\n\n"
        f"Market data (1H primary + 4H/1D context, indicators):\n"
        f"{json.dumps(features, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Based on the data above and the rules, decide. Respond with ONE JSON object:\n"
        "{\n"
        '  "decision": "ENTER_LONG" | "ENTER_SHORT" | "NO_TRADE",\n'
        '  "reference_price": float (current price you enter at; null if NO_TRADE),\n'
        '  "stop_price": float (structural stop; null if NO_TRADE),\n'
        '  "target_price": float (next structural level; null if NO_TRADE),\n'
        '  "leverage": integer 1-7 (null if NO_TRADE),\n'
        '  "confidence": float 0.0-1.0,\n'
        '  "next_recheck_hours": float 0.5-12,\n'
        '  "reason_short": "한국어 1-2문장: 어떤 추세·근거로 이 결정인지"\n'
        "}\n"
        "For ENTER_LONG require stop<reference<target; for ENTER_SHORT require "
        "target<reference<stop. JSON only."
    )


def build_recheck_prompt(symbol: str, position: dict, features: dict, mark: float) -> str:
    side = position["side"]; entry = position["entry_price"]
    sl = position["stop_price"]; tp = position.get("target_price")
    return (
        f"Symbol: {symbol}  (OPEN {('LONG' if side=='Buy' else 'SHORT')})\n"
        f"Entry {entry} · Stop {sl} · Target {tp} · Current {mark}\n\n"
        f"Market data now:\n{json.dumps(features, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Decide management. Respond with ONE JSON object:\n"
        "{\n"
        '  "decision": "HOLD" | "MODIFY" | "EXIT",\n'
        '  "new_stop_price": float | null,\n'
        '  "new_target_price": float | null,\n'
        '  "next_recheck_hours": float 0.5-12,\n'
        '  "reason_short": "한국어 1문장"\n'
        "}\n"
        "MODIFY only with a structural reason; never widen the stop. JSON only."
    )
