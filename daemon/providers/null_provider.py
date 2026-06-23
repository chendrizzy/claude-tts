"""NullProvider — no-LLM deterministic floor (first-class).

judge() always SKIPs (the deterministic floor in the router decides what the
structured extractors surface). summarize() reuses the existing rule-based
fallback (normalize + sentence-truncate). Keeps TTS useful with zero model.
"""
from __future__ import annotations

from typing import Optional

from daemon.providers.base import LLMProvider
from daemon.ollama_summarizer import rule_based_summary
from daemon.tts_types import Category


class NullProvider(LLMProvider):
    @property
    def inner_timeout_s(self) -> float:
        return 0.0  # no network call

    async def judge(self, snippet: str, tool_name: str, context: str = "") -> bool:
        return False

    async def summarize(
        self,
        content: str,
        category: Category,
        context_hint: str = "",
        allow_fallback: bool = True,
    ) -> Optional[str]:
        return rule_based_summary(content)
