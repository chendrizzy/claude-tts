"""OpenAICompatProvider — any /v1/chat/completions endpoint (stdlib urllib).

Default-SKIP on any failure (network, parse) to honor the conservative judge
contract. Not the default backend; calibration-validated in the plugin phase.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Optional

from daemon.providers.base import LLMProvider
from daemon.providers.prompts import build_judge_hint
from daemon.tts_types import Category

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 8.0,
        max_tokens: int = 120,
        temperature: float = 0.3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._temperature = temperature

    @property
    def inner_timeout_s(self) -> float:
        return float(self._timeout_s)

    def _chat_sync(self, prompt: str) -> Optional[str]:
        body = json.dumps({
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions", data=body, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("openai_compat call failed: %s", exc)
            return None

    async def _chat(self, prompt: str) -> Optional[str]:
        return await asyncio.get_event_loop().run_in_executor(None, self._chat_sync, prompt)

    async def judge(self, snippet: str, tool_name: str, context: str = "") -> bool:
        prompt = f"{build_judge_hint(tool_name, context)}\n\nOUTPUT:\n{snippet[:600]}"
        verdict = await self._chat(prompt)
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
        prompt = (
            f"Summarize the following {category.value} output for a brief spoken "
            f"text-to-speech readout. One or two plain sentences, no markup. "
            f"{context_hint}\n\nCONTENT:\n{content}"
        )
        out = await self._chat(prompt)
        if out is None and not allow_fallback:
            return None
        return out
