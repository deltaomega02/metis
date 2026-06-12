# ai/__init__.py
from .gemini_client import GeminiClient, gemini_client
from .prompts import (
    create_entry_filter_prompt,
    create_phase4_recheck_prompt
)