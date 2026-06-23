"""LLMProvider — the SPEAK/SKIP judge + content summarizer abstraction.

Implementations: OllamaProvider (default, local), NullProvider (no-LLM
deterministic floor), OpenAICompatProvider (bring-your-own model). The
deterministic floor (_drop_check_raw / is_speakable / normalize_for_speech)
is NOT part of this interface — it stays in the router/text_utils as the
last-chokepoint guarantee.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from daemon.tts_types import Category


class LLMProvider(ABC):
    """Unified judge + summarize provider."""

    @property
    def inner_timeout_s(self) -> float:
        """The provider's own inner call budget, in seconds.

        ContentRouter reads this to clamp its outer wrapper timeouts above the
        inner cap (so a slow call is never cancelled before the markdown-clean
        fallback runs). Defaults to 2.0 for providers with no network call.
        """
        return 2.0

    @abstractmethod
    async def judge(self, snippet: str, tool_name: str, context: str = "") -> bool:
        """Return True iff `snippet` should be spoken. Defaults to False (SKIP)
        on any failure or ambiguity — callers rely on conservative SKIP."""
        raise NotImplementedError

    @abstractmethod
    async def summarize(
        self,
        content: str,
        category: Category,
        context_hint: str = "",
        allow_fallback: bool = True,
    ) -> Optional[str]:
        """Return a TTS-ready summary of `content`. When `allow_fallback` is
        False, return None (not a truncation) on timeout — the judge relies on
        None to avoid mis-parsing a fallback as a verdict."""
        raise NotImplementedError
