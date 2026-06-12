"""Gemini Flash client for the decision call.

One call per symbol per cycle. The client:

- Uses the google-genai SDK (>= 1.0).
- Sends the full prompt (system + static + dynamic blocks) every call. Gemini 2.5+
  applies *implicit context caching* automatically when the shared prefix is large
  enough, delivering the same ~75% token discount as explicit caching, but without
  the TTL/expiry/PermissionDenied failure modes that come with manually-managed
  cache resources. (Verified against Google AI for Developers docs, 2026.)
- Requests the native ``responseJsonSchema`` derived from the Pydantic model so
  the JSON output is constrained at decode time.
- Validates the response with Pydantic; on schema failure it retries once with a
  corrective hint and otherwise falls back to NO_TRADE.
- Honors a hard ``TIMEOUT_SEC`` — exceeding it returns an error result so the
  caller forces NO_TRADE for that cycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
from pydantic import ValidationError

from ai.prompt_registry import (
    PROMPT_VERSION,
    PromptBundle,
    assemble_user_prompt,
    build_active_prompt,
)
from ai.response_schema import MetisDecision, gemini_response_schema_dict
from config.settings import GEMINI

logger = logging.getLogger("metis.gemini_client")


@dataclass
class GeminiCallResult:
    """Outcome of one Gemini call. ``decision`` is None on hard failure."""
    decision: Optional[MetisDecision]
    raw_text: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    finish_reason: str
    retry_count: int
    error: Optional[str]
    cache_hit: bool  # derived from cached_tokens > 0


class GeminiClient:
    """Process-wide Gemini Flash client. Implicit-cache aware.

    Note: implicit caching is fully managed by Google. We just send the full prompt
    every call with a stable prefix; Gemini deduplicates server-side and reports
    ``cached_content_token_count`` in usage metadata so we can log hit rate.
    """

    def __init__(self):
        self._client = genai.Client(api_key=GEMINI.API_KEY)
        self._bundle: PromptBundle = build_active_prompt()

    async def decide(
        self,
        runtime_anchor_json: str,
        runtime_features_json: str,
        runtime_controls_json: str,
    ) -> GeminiCallResult:
        """Run one decision call and return a validated MetisDecision (or None on hard failure)."""
        bundle = self._bundle
        # Always send the full prompt. The stable prefix (system + static blocks)
        # lets Gemini's implicit cache fire automatically; only the runtime tail
        # changes between calls.
        send_user = assemble_user_prompt(
            bundle,
            runtime_anchor_json=runtime_anchor_json,
            runtime_features_json=runtime_features_json,
            runtime_controls_json=runtime_controls_json,
        )

        schema = gemini_response_schema_dict()
        last_error: Optional[str] = None
        retries = 0
        t0 = time.time()

        while retries <= GEMINI.MAX_RETRIES:
            try:
                cfg_kwargs = {
                    "system_instruction": bundle.system_instruction,
                    "temperature": GEMINI.TEMPERATURE,
                    "max_output_tokens": GEMINI.MAX_OUTPUT_TOKENS,
                    "response_mime_type": "application/json",
                    "response_json_schema": schema,
                }
                # thinking_config — Gemini 3 exposes thinking_level; older SDKs use
                # thinking_budget. Try the new form, fall back, otherwise skip.
                try:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                        thinking_level=GEMINI.THINKING_LEVEL,
                        include_thoughts=False,
                    )
                except (TypeError, ValueError):
                    try:
                        budget = {"high": 8192, "medium": 4096, "low": 1024, "none": 0}.get(
                            GEMINI.THINKING_LEVEL, 4096
                        )
                        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                            thinking_budget=budget, include_thoughts=False,
                        )
                    except Exception:
                        pass

                cfg = types.GenerateContentConfig(**cfg_kwargs)

                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.models.generate_content,
                        model=GEMINI.MODEL_ID,
                        contents=send_user,
                        config=cfg,
                    ),
                    timeout=GEMINI.TIMEOUT_SEC,
                )

                text = response.text or ""
                usage = getattr(response, "usage_metadata", None)
                in_tok = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
                out_tok = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
                cached_tok = int(getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0
                finish = "STOP"
                if hasattr(response, "candidates") and response.candidates:
                    finish = str(getattr(response.candidates[0], "finish_reason", "STOP"))

                try:
                    parsed = json.loads(text)
                    decision = MetisDecision(**parsed)
                    return GeminiCallResult(
                        decision=decision, raw_text=text,
                        input_tokens=in_tok, output_tokens=out_tok, cached_tokens=cached_tok,
                        latency_ms=int((time.time() - t0) * 1000),
                        finish_reason=finish, retry_count=retries, error=None,
                        cache_hit=cached_tok > 0,
                    )
                except (json.JSONDecodeError, ValidationError) as ve:
                    last_error = f"validation_failed: {type(ve).__name__}: {str(ve)[:200]}"
                    logger.warning(f"Gemini validation fail (retry={retries}): {last_error}")
                    if retries >= GEMINI.MAX_RETRIES:
                        break
                    send_user = (
                        send_user
                        + "\n\nPrevious response failed validation: "
                        + str(ve)[:300]
                        + ". Re-evaluate from the supplied features, do not repair the old text. "
                        "Return exactly one schema-valid JSON object. If any market condition is "
                        "uncertain, choose NO_TRADE. Do not output anything outside JSON."
                    )
                    retries += 1
                    continue

            except asyncio.TimeoutError:
                last_error = "timeout"
                logger.warning(f"Gemini timeout after {GEMINI.TIMEOUT_SEC}s")
                break
            except Exception as e:
                last_error = f"api_error:{type(e).__name__}:{str(e)[:200]}"
                logger.warning(f"Gemini error: {last_error}")
                if retries >= GEMINI.MAX_RETRIES:
                    break
                retries += 1
                continue

        return GeminiCallResult(
            decision=None, raw_text="",
            input_tokens=0, output_tokens=0, cached_tokens=0,
            latency_ms=int((time.time() - t0) * 1000),
            finish_reason="ERROR", retry_count=retries, error=last_error,
            cache_hit=False,
        )


# ── Singleton ──
_gemini_singleton: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    global _gemini_singleton
    if _gemini_singleton is None:
        _gemini_singleton = GeminiClient()
    return _gemini_singleton
