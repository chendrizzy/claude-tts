"""Select an LLMProvider from config. Default: local Ollama."""
from __future__ import annotations

from daemon.providers.base import LLMProvider
from daemon.providers.null_provider import NullProvider
from daemon.providers.ollama_provider import OllamaProvider
from daemon.providers.openai_compat import OpenAICompatProvider


def make_provider(config: dict, summarizer) -> LLMProvider:
    cfg = (config or {}).get("llm_provider", {}) if isinstance(config, dict) else {}
    kind = str(cfg.get("type", "ollama")).lower()

    if kind == "null":
        return NullProvider()
    if kind == "openai":
        return OpenAICompatProvider(
            base_url=str(cfg.get("base_url", "")),
            model=str(cfg.get("model", "")),
            api_key=str(cfg.get("api_key", "")),
            timeout_s=float(cfg.get("timeout_s", 8.0)),
        )
    # default: ollama — but a missing summarizer means Ollama init failed.
    if summarizer is None:
        return NullProvider()
    return OllamaProvider(summarizer)
