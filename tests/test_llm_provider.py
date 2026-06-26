"""Unit tests for the LLMProvider seam (judge + summarize abstraction)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from daemon.providers.base import LLMProvider


def test_llmprovider_is_abstract():
    with pytest.raises(TypeError):
        LLMProvider()  # abstract — cannot instantiate


def test_llmprovider_declares_judge_and_summarize():
    assert hasattr(LLMProvider, "judge")
    assert hasattr(LLMProvider, "summarize")
    assert hasattr(LLMProvider, "inner_timeout_s")


import asyncio

from daemon.providers.ollama_provider import OllamaProvider
from daemon.tts_types import Category


class _MockSummarizer:
    """Mirror of tests/test_content_router.py:MockOllamaSummarizer."""

    def __init__(self, response="[mock]", timeout_s=3.5):
        self.response = response
        self._timeout_s = timeout_s
        self.calls = []

    async def summarize(self, content, category, context_hint="", allow_fallback=True):
        self.calls.append((content, category, context_hint, allow_fallback))
        return self.response


def test_ollama_judge_true_on_speak_token():
    p = OllamaProvider(_MockSummarizer(response="SPEAK"))
    assert asyncio.run(p.judge("42 tests passed", "Bash", "ran pytest")) is True


def test_ollama_judge_false_on_skip_and_empty_and_none():
    assert asyncio.run(OllamaProvider(_MockSummarizer("SKIP")).judge("x", "Bash")) is False
    assert asyncio.run(OllamaProvider(_MockSummarizer("")).judge("x", "Bash")) is False
    assert asyncio.run(OllamaProvider(_MockSummarizer(None)).judge("x", "Bash")) is False


def test_ollama_judge_calls_summarize_with_status_no_fallback():
    m = _MockSummarizer("SPEAK")
    asyncio.run(OllamaProvider(m).judge("out", "Bash", "ctx"))
    content, category, hint, allow_fallback = m.calls[0]
    assert category == Category.STATUS
    assert allow_fallback is False
    assert "BINARY_JUDGMENT" in hint and "Bash" in hint and "ctx" in hint


def test_ollama_summarize_delegates():
    m = _MockSummarizer("a summary")
    out = asyncio.run(OllamaProvider(m).summarize("long content", Category.ERROR, "h"))
    assert out == "a summary"
    assert m.calls[0] == ("long content", Category.ERROR, "h", True)


def test_ollama_inner_timeout_s_reflects_wrapped_summarizer():
    assert OllamaProvider(_MockSummarizer(timeout_s=3.5)).inner_timeout_s == 3.5


from daemon.providers.null_provider import NullProvider
from daemon.ollama_summarizer import rule_based_summary


def test_null_judge_always_false():
    assert asyncio.run(NullProvider().judge("anything at all", "Bash", "ctx")) is False


def test_null_summarize_matches_rule_based_summary():
    text = "The build passed. All forty-two tests are green. Coverage rose to 91 percent."
    out = asyncio.run(NullProvider().summarize(text, Category.STATUS))
    assert out == rule_based_summary(text)
    assert out  # non-empty for real prose


def test_null_inner_timeout_is_zero():
    assert NullProvider().inner_timeout_s == 0.0


import json
from unittest.mock import patch

from daemon.providers.openai_compat import OpenAICompatProvider


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_chat(content, capture):
    payload = {"choices": [{"message": {"content": content}}]}

    def _inner(req, timeout=None):
        data = getattr(req, "data", None)
        if data:
            capture["body"] = json.loads(data.decode())
            capture["url"] = req.full_url
        return _Resp(json.dumps(payload).encode())

    return _inner


def test_openai_judge_parses_speak():
    cap = {}
    p = OpenAICompatProvider(base_url="http://localhost:1234/v1", model="m", api_key="k")
    with patch("urllib.request.urlopen", _fake_chat("SPEAK", cap)):
        assert asyncio.run(p.judge("42 passed", "Bash", "ctx")) is True
    assert cap["url"].endswith("/chat/completions")
    assert cap["body"]["model"] == "m"


def test_openai_judge_false_on_skip():
    p = OpenAICompatProvider(base_url="http://localhost:1234/v1", model="m", api_key="k")
    with patch("urllib.request.urlopen", _fake_chat("SKIP", {})):
        assert asyncio.run(p.judge("noise", "Bash")) is False


def test_openai_summarize_returns_content():
    p = OpenAICompatProvider(base_url="http://localhost:1234/v1", model="m", api_key="k")
    with patch("urllib.request.urlopen", _fake_chat("short summary", {})):
        out = asyncio.run(p.summarize("long text", Category.STATUS, "h"))
    assert out == "short summary"


from daemon.providers.factory import make_provider


def test_factory_defaults_to_ollama():
    p = make_provider({}, _MockSummarizer())
    assert isinstance(p, OllamaProvider)


def test_factory_ollama_with_no_summarizer_falls_back_to_null():
    p = make_provider({"llm_provider": {"type": "ollama"}}, None)
    assert isinstance(p, NullProvider)


def test_factory_explicit_null():
    p = make_provider({"llm_provider": {"type": "null"}}, _MockSummarizer())
    assert isinstance(p, NullProvider)


def test_factory_openai():
    cfg = {"llm_provider": {"type": "openai", "base_url": "http://x/v1", "model": "m", "api_key": "k"}}
    p = make_provider(cfg, None)
    assert isinstance(p, OpenAICompatProvider)


# --- OLLAMA_HOST env var resolution (daemon/ollama_integration._ollama_api_base) ---

import os
from daemon.ollama_integration import _ollama_api_base


def test_ollama_host_env_resolution(monkeypatch):
    # Unset → local default.
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert _ollama_api_base() == "http://localhost:11434"
    # Full URL → used as-is (trailing slash trimmed).
    monkeypatch.setenv("OLLAMA_HOST", "http://10.0.0.5:11434/")
    assert _ollama_api_base() == "http://10.0.0.5:11434"
    # Bare host:port → http:// assumed (Ollama's own convention).
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:11434")
    assert _ollama_api_base() == "http://10.0.0.5:11434"
    # https honored.
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.com")
    assert _ollama_api_base() == "https://ollama.example.com"
    # Blank → default.
    monkeypatch.setenv("OLLAMA_HOST", "  ")
    assert _ollama_api_base() == "http://localhost:11434"
