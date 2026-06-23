"""OllamaProvider — wraps the existing OllamaSummarizer.

summarize() delegates unchanged. judge() holds the relocated body of
ContentRouter._binary_llm_judge (build hint -> summarize STATUS, no fallback
-> parse first token). The outer time budget and default-SKIP-on-failure stay
in ContentRouter, which wraps this in asyncio.wait_for.
"""
from __future__ import annotations

from typing import Optional

from daemon.providers.base import LLMProvider
from daemon.providers.prompts import build_judge_hint
from daemon.tts_types import Category


class OllamaProvider(LLMProvider):
    def __init__(self, summarizer) -> None:
        self._summarizer = summarizer

    @property
    def inner_timeout_s(self) -> float:
        return float(getattr(self._summarizer, "_timeout_s", 2.0) or 2.0)

    async def judge(self, snippet: str, tool_name: str, context: str = "") -> bool:
        if self._summarizer is None:
            return False
        # allow_fallback=False: on inner timeout the summarizer returns None
        # (not a truncation of the bash stdout), so a fallback is never parsed
        # as a verdict.
        verdict = await self._summarizer.summarize(
            snippet[:600], Category.STATUS, build_judge_hint(tool_name, context),
            allow_fallback=False,
        )
        if not verdict:
            return False
        token = verdict.strip().split()[0].upper().rstrip(".,!?:")
        return token == "SPEAK"

    async def summarize(
        self,
        content: str,
        category: Category,
        context_hint: str = "",
        allow_fallback: bool = True,
    ) -> Optional[str]:
        if self._summarizer is None:
            return None
        return await self._summarizer.summarize(
            content, category, context_hint, allow_fallback
        )
