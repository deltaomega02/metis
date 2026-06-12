"""Gemini Flash client for the decision and recheck calls.

One call per symbol per cycle (and per open-position recheck). Sends a stable
system instruction plus the runtime data, requests JSON, validates that the
expected keys are present, and retries once on parse failure before giving up
(caller then treats it as NO_TRADE / HOLD). Implicit context caching applies to
the stable system+rule prefix automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

from config.settings import GEMINI

logger = logging.getLogger("metis.gemini")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class AIResult:
    data: Optional[dict]      # parsed JSON, or None on hard failure
    latency_ms: int
    error: Optional[str]


class GeminiClient:
    def __init__(self):
        self._client = genai.Client(api_key=GEMINI.API_KEY)

    async def ask(self, system: str, prompt: str, required_keys: tuple) -> AIResult:
        t0 = time.time()
        send = prompt
        last_err = None
        for attempt in range(GEMINI.MAX_RETRIES + 1):
            try:
                cfg_kwargs = {
                    "system_instruction": system,
                    "temperature": GEMINI.TEMPERATURE,
                    "max_output_tokens": GEMINI.MAX_OUTPUT_TOKENS,
                    "response_mime_type": "application/json",
                }
                try:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                        thinking_level=GEMINI.THINKING_LEVEL, include_thoughts=False)
                except (TypeError, ValueError):
                    pass
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.models.generate_content,
                        model=GEMINI.MODEL_ID, contents=send,
                        config=types.GenerateContentConfig(**cfg_kwargs)),
                    timeout=GEMINI.TIMEOUT_SEC)
                data = self._parse(resp.text or "")
                if data and all(k in data for k in required_keys):
                    return AIResult(data, int((time.time() - t0) * 1000), None)
                last_err = f"missing_keys_or_unparseable"
            except asyncio.TimeoutError:
                last_err = "timeout"; break
            except Exception as e:
                last_err = f"{type(e).__name__}:{str(e)[:160]}"
            send = prompt + "\n\nPrevious reply was invalid. Return exactly one JSON object with all required keys."
        return AIResult(None, int((time.time() - t0) * 1000), last_err)

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        try:
            return json.loads(text)
        except Exception:
            m = _JSON_RE.search(text)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
        return None


_client: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client
