"""Tests for daemon.ollama_summarizer.

Mock-based tests verify:
- async wrapping of the sync OllamaClient
- timeout fallback to rule-based shortener
- contraction preservation through the prompt path
- latency tracking inside the 60s window
- batch condensation + fallback
- warmup success and timeout paths

Integration tests (skip-if-no-model) exercise the real prompt against
worked-example inputs from the plan. They MUST skip cleanly if Ollama is
not running or the `model` model is not yet built (W1.A territory).

Run mock-only:
    pytest tests/test_ollama_summarizer.py -v -m "not integration"

Run all (requires Ollama + model):
    pytest tests/test_ollama_summarizer.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the project root importable when running pytest from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from daemon.ollama_summarizer import OllamaSummarizer  # noqa: E402
from daemon.tts_types import (  # noqa: E402
    Category,
    PRIORITY_NORMAL,
    RouterDecision,
    RoutedItem,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _make_routed_item(content: str, category: Category = Category.STATUS) -> RoutedItem:
    decision = RouterDecision(
        should_speak=True,
        category=category,
        content=content,
        priority=PRIORITY_NORMAL,
        source_event_id=f"evt-{abs(hash(content)) % 10_000}",
        classified_at=time.time(),
    )
    return RoutedItem(decision=decision, session_id="test-session")


class _MockOllamaClient:
    """Minimal stand-in for OllamaClient.

    Records calls + lets each test program a response (or sleep + response,
    or exception) per generate_response invocation.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._responder = lambda prompt, model, max_tokens, temperature: ""

    def set_responder(self, fn) -> None:
        self._responder = fn

    def generate_response(
        self, prompt, model=None, max_tokens=500, temperature=0.7, keep_alive=None
    ):  # mirrors real signature (keep_alive added in R2)
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "keep_alive": keep_alive,
            }
        )
        return self._responder(prompt, model, max_tokens, temperature)


def _ollama_model_available() -> bool:
    """Best-effort check: does Ollama respond AND list a `model` model?"""
    try:
        import requests
    except ImportError:
        return False
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    try:
        models = r.json().get("models", [])
    except Exception:
        return False
    names = {m.get("name", "") for m in models}
    # Accept either bare "qwen2.5-coder:1.5b" or a tagged variant like "model:latest".
    return any(n == "qwen2.5-coder:1.5b" or n.startswith("model:") for n in names)


_MODEL_AVAILABLE = _ollama_model_available()
_skip_no_model = pytest.mark.skipif(
    not _MODEL_AVAILABLE,
    reason="Ollama not running or 'model' model not built (W1.A pulls it)",
)


# --------------------------------------------------------------------------- #
# Mock-based tests                                                            #
# --------------------------------------------------------------------------- #

class TestSummarizeBasic:
    @pytest.mark.asyncio
    async def test_returns_ollama_response_stripped(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "  Twenty-three passed, four failed.  \n")
        summarizer = OllamaSummarizer(client, model="qwen2.5-coder:1.5b", timeout_s=2.0)

        result = await summarizer.summarize(
            "23 passed, 4 failed in 12.3s",
            Category.STATUS,
            context_hint="test result",
        )

        assert result == "Twenty-three passed, four failed."
        assert len(client.calls) == 1
        assert client.calls[0]["model"] == "qwen2.5-coder:1.5b"
        assert client.calls[0]["temperature"] == 0.3  # deterministic
        assert client.calls[0]["max_tokens"] == 250

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "should-not-be-called")
        summarizer = OllamaSummarizer(client)

        assert await summarizer.summarize("", Category.STATUS) == ""
        assert await summarizer.summarize("   ", Category.STATUS) == ""
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_prompt_carries_category_and_context(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "ok")
        summarizer = OllamaSummarizer(client)

        await summarizer.summarize(
            "the build broke",
            Category.ERROR,
            context_hint="cargo output",
        )

        prompt = client.calls[0]["prompt"]
        assert "CATEGORY: error" in prompt
        assert "CONTEXT: cargo output" in prompt
        assert "CONTENT: the build broke" in prompt

    @pytest.mark.asyncio
    async def test_context_hint_default_when_blank(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "ok")
        summarizer = OllamaSummarizer(client)

        await summarizer.summarize("hello", Category.INSIGHT)
        assert "CONTEXT: (none)" in client.calls[0]["prompt"]


class TestTimeoutFallback:
    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_rule_based(self):
        client = _MockOllamaClient()

        def slow(prompt, model, max_tokens, temperature):
            # Sync sleep in the worker thread — wait_for must trip.
            time.sleep(0.5)
            return "would have summarized"

        client.set_responder(slow)
        summarizer = OllamaSummarizer(client, model="qwen2.5-coder:1.5b", timeout_s=0.05)

        long_input = (
            "The race condition only fires when the cache is warm because "
            "the eviction path skips the lock check. Reproducing it requires "
            "two concurrent reads on the same key after a warmup pass."
        )
        result = await summarizer.summarize(long_input, Category.INSIGHT)

        # Fallback returns the (possibly truncated) original content.
        assert result.startswith("The race condition")
        assert "would have summarized" not in result

    @pytest.mark.asyncio
    async def test_exception_falls_back_quietly(self):
        client = _MockOllamaClient()

        def boom(*a, **kw):
            raise ConnectionError("ollama down")

        client.set_responder(boom)
        summarizer = OllamaSummarizer(client, timeout_s=1.0)

        result = await summarizer.summarize(
            "Deployment finished after 12 minutes.", Category.STATUS
        )
        assert "Deployment finished" in result

    @pytest.mark.asyncio
    async def test_none_response_falls_back(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)
        summarizer = OllamaSummarizer(client, timeout_s=1.0)

        result = await summarizer.summarize("Just a fact.", Category.STATUS)
        assert result == "Just a fact."

    @pytest.mark.asyncio
    async def test_empty_response_falls_back(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "   \n  ")
        summarizer = OllamaSummarizer(client, timeout_s=1.0)

        result = await summarizer.summarize("Compiled with 3 warnings.", Category.STATUS)
        assert "Compiled with 3 warnings" in result


class TestRuleBasedFallback:
    """Fallback shortener behaviour — these test the deterministic path."""

    @pytest.mark.asyncio
    async def test_strips_code_blocks(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)  # force fallback
        summarizer = OllamaSummarizer(client)

        content = "Found the bug ```python\ndef oops(): pass\n``` in handler.py."
        result = await summarizer.summarize(content, Category.INSIGHT)
        assert "def oops" not in result
        assert "Found the bug" in result

    @pytest.mark.asyncio
    async def test_truncates_at_sentence_boundary(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)
        summarizer = OllamaSummarizer(client)

        # Build content >200 chars with clear sentence breaks.
        sentences = (
            "First sentence here. Second sentence here. Third sentence here. "
            "Fourth sentence here. Fifth sentence here. Sixth sentence here. "
            "Seventh sentence here. Eighth sentence here. Ninth sentence here."
        )
        result = await summarizer.summarize(sentences, Category.INSIGHT)
        assert len(result) <= 210  # ~200 + small slack, no mid-word cut
        assert result.endswith(".") or result.endswith("...")

    @pytest.mark.asyncio
    async def test_preserves_contractions(self):
        """Critical invariant: don't / can't / it's must round-trip."""
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)  # force fallback path
        summarizer = OllamaSummarizer(client)

        content = "I can't reproduce it locally — don't think it's flaky."
        result = await summarizer.summarize(content, Category.INSIGHT)
        assert "can't" in result
        assert "don't" in result
        assert "it's" in result
        # And the expanded forms must NOT appear
        assert "do not" not in result
        assert "cannot" not in result

    @pytest.mark.asyncio
    async def test_short_input_passes_through_fallback_unchanged(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)
        summarizer = OllamaSummarizer(client)

        result = await summarizer.summarize("Build OK.", Category.STATUS)
        assert result == "Build OK."

    @pytest.mark.asyncio
    async def test_empty_after_stripping_returns_empty(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)
        summarizer = OllamaSummarizer(client)

        result = await summarizer.summarize("```only code```", Category.STATUS)
        assert result == ""


class TestCondenseBatch:
    @pytest.mark.asyncio
    async def test_batch_calls_ollama_with_count_and_category(self):
        client = _MockOllamaClient()
        client.set_responder(
            lambda *a, **kw: "Tests passed, build green, deploy succeeded."
        )
        summarizer = OllamaSummarizer(client)

        items = [
            _make_routed_item("23 passed, 4 failed.", Category.STATUS),
            _make_routed_item("Build compiled in 12s.", Category.STATUS),
            _make_routed_item("Deployment finished.", Category.STATUS),
        ]
        result = await summarizer.condense_batch(items)

        assert result == "Tests passed, build green, deploy succeeded."
        prompt = client.calls[0]["prompt"]
        assert "Combine these 3 status updates" in prompt
        assert "ONE spoken sentence" in prompt
        assert "max 25 words" in prompt
        # All three contents should be in the prompt
        assert "23 passed, 4 failed." in prompt
        assert "Build compiled in 12s." in prompt
        assert "Deployment finished." in prompt

    @pytest.mark.asyncio
    async def test_batch_empty_returns_empty(self):
        client = _MockOllamaClient()
        summarizer = OllamaSummarizer(client)

        assert await summarizer.condense_batch([]) == ""
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_batch_single_item_returns_content_no_call(self):
        client = _MockOllamaClient()
        summarizer = OllamaSummarizer(client)

        item = _make_routed_item("Just one thing.", Category.STATUS)
        result = await summarizer.condense_batch([item])
        assert result == "Just one thing."
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_batch_timeout_falls_back_to_join(self):
        client = _MockOllamaClient()

        def slow(*a, **kw):
            time.sleep(0.3)
            return "should not see this"

        client.set_responder(slow)
        summarizer = OllamaSummarizer(client, timeout_s=0.05)

        items = [
            _make_routed_item("First fact.", Category.STATUS),
            _make_routed_item("Second fact.", Category.STATUS),
        ]
        result = await summarizer.condense_batch(items)
        # Fallback joins with '. ' (rule-based shortener strips trailing dot, re-joins)
        assert "First fact" in result
        assert "Second fact" in result
        assert "should not see this" not in result

    @pytest.mark.asyncio
    async def test_batch_fallback_truncates_to_200(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: None)  # force fallback
        summarizer = OllamaSummarizer(client)

        # Build many items so the joined string blows past 200 chars.
        items = [
            _make_routed_item(f"Long fact number {i} with extra padding text here.")
            for i in range(15)
        ]
        result = await summarizer.condense_batch(items)
        assert len(result) <= 210


class TestLatencyTracking:
    @pytest.mark.asyncio
    async def test_avg_latency_zero_at_start(self):
        client = _MockOllamaClient()
        summarizer = OllamaSummarizer(client)
        assert summarizer.avg_latency_ms == 0.0

    @pytest.mark.asyncio
    async def test_avg_latency_records_successful_calls(self):
        client = _MockOllamaClient()

        def slow_ish(*a, **kw):
            time.sleep(0.02)  # ~20ms
            return "ok"

        client.set_responder(slow_ish)
        summarizer = OllamaSummarizer(client, timeout_s=2.0)

        for _ in range(3):
            await summarizer.summarize("hello", Category.STATUS)

        avg = summarizer.avg_latency_ms
        # We slept 20ms per call; expect at least ~10ms recorded (be lax for CI).
        assert avg > 5.0, f"expected >5ms avg, got {avg}"
        assert avg < 1500.0  # sanity upper bound

    @pytest.mark.asyncio
    async def test_avg_latency_ignores_failures(self):
        client = _MockOllamaClient()

        # First a failure (raises), then a success.
        responses = iter([ConnectionError("nope"), "ok"])

        def alternator(*a, **kw):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        client.set_responder(alternator)
        summarizer = OllamaSummarizer(client, timeout_s=2.0)

        await summarizer.summarize("x", Category.STATUS)  # fails
        await summarizer.summarize("y", Category.STATUS)  # succeeds

        # Only the success contributed; avg > 0.
        assert summarizer.avg_latency_ms > 0.0

    @pytest.mark.asyncio
    async def test_avg_latency_evicts_stale_samples(self, monkeypatch):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "ok")
        summarizer = OllamaSummarizer(client, timeout_s=2.0)

        await summarizer.summarize("a", Category.STATUS)
        assert summarizer.avg_latency_ms > 0.0

        # Force the latency log to look 120s old by rewriting tuples.
        from collections import deque

        old = list(summarizer._latency_log)
        summarizer._latency_log = deque(
            (ts - 120.0, lat) for ts, lat in old
        )

        # avg_latency_ms must evict and report 0.
        assert summarizer.avg_latency_ms == 0.0


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warmup_success(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "hi")
        summarizer = OllamaSummarizer(client, model="qwen2.5-coder:1.5b")

        assert await summarizer.warmup() is True
        # Warmup uses the model name we passed.
        assert client.calls[0]["model"] == "qwen2.5-coder:1.5b"
        # Tiny token budget.
        assert client.calls[0]["max_tokens"] <= 8

    @pytest.mark.asyncio
    async def test_warmup_returns_false_on_empty(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: "")
        summarizer = OllamaSummarizer(client)
        assert await summarizer.warmup() is False

    @pytest.mark.asyncio
    async def test_warmup_returns_false_on_exception(self):
        client = _MockOllamaClient()
        client.set_responder(lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        summarizer = OllamaSummarizer(client)
        assert await summarizer.warmup() is False

    @pytest.mark.asyncio
    async def test_warmup_returns_false_on_timeout(self, monkeypatch):
        """Warmup uses a longer ceiling than steady-state; force a tight
        wait_for inside the module to simulate a slow first generation.
        """
        client = _MockOllamaClient()

        def really_slow(*a, **kw):
            time.sleep(2.0)
            return "ok"

        client.set_responder(really_slow)
        summarizer = OllamaSummarizer(client, timeout_s=0.001)

        from daemon import ollama_summarizer as mod

        real_wait_for = mod.asyncio.wait_for

        async def tight_wait_for(coro, timeout):
            return await real_wait_for(coro, timeout=0.05)

        monkeypatch.setattr(mod.asyncio, "wait_for", tight_wait_for)
        assert await summarizer.warmup() is False


# --------------------------------------------------------------------------- #
# Integration tests — require a running Ollama with `model` model            #
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestIntegrationModel:
    """Worked-example tests against the real model model.

    These are aspirational: by the time anyone runs the full suite, W1.A
    should have built model. Until then they skip cleanly.
    """

    def _make_real_summarizer(self) -> OllamaSummarizer:
        from daemon.ollama_integration import OllamaClient

        client = OllamaClient()
        # Use a generous timeout for integration; we're testing CORRECTNESS
        # not the 1.0s production budget here.
        return OllamaSummarizer(client, model="qwen2.5-coder:1.5b", timeout_s=15.0)

    @_skip_no_model
    @pytest.mark.asyncio
    async def test_pytest_output_summary(self):
        summarizer = self._make_real_summarizer()
        result = await summarizer.summarize(
            "23 passed, 4 failed in 12.3s",
            Category.STATUS,
            context_hint="pytest result",
        )
        # Accept either word or numeric form. Model may insert "tests" between
        # the count and "passed" (e.g., "twenty-three tests passed") — verify
        # presence of both tokens in the output rather than adjacency.
        lower = result.lower()
        passed_present = (
            ("twenty-three" in lower or "23" in lower)
            and "passed" in lower
        )
        failed_present = (
            ("four" in lower or "4 " in lower or " 4." in lower)
            and "failed" in lower
        )
        assert passed_present, f"missing pass count in: {result!r}"
        assert failed_present, f"missing fail count in: {result!r}"

    @_skip_no_model
    @pytest.mark.asyncio
    async def test_bash_command_not_found(self):
        summarizer = self._make_real_summarizer()
        result = await summarizer.summarize(
            "cargo: command not found",
            Category.ERROR,
            context_hint="bash exit 127",
        )
        lower = result.lower()
        assert "cargo" in lower, f"missing 'cargo' in: {result!r}"
        not_found_phrases = ("not found", "can't find", "cannot find", "missing")
        assert any(p in lower for p in not_found_phrases), (
            f"no 'not found'-ish phrase in: {result!r}"
        )

    @_skip_no_model
    @pytest.mark.asyncio
    async def test_long_insight_stays_within_three_sentences(self):
        # Plan rule: "complex finding → three sentences max". Model's prompt
        # caps SENTENCE COUNT, not characters; three substantive sentences can
        # exceed 250 chars while still being TTS-appropriate (<~45 sec spoken).
        # We assert sentence count + a soft 500-char ceiling (tweet-read-aloud).
        summarizer = self._make_real_summarizer()
        long_insight = (
            "After tracing through the request lifecycle, the race condition "
            "only fires with a warm cache because the eviction path skips the "
            "lock check, leaving the entry visible to the next reader for a "
            "few microseconds. The fix is to acquire the lock before the "
            "visibility test, even when the entry is about to be evicted, "
            "since eviction itself races with concurrent reads in this region."
        )
        result = await summarizer.summarize(long_insight, Category.INSIGHT)
        assert result, "summary was empty"
        # Count sentence-terminating punctuation (.!?). Three is the prompt cap;
        # allow up to 4 for benign trailing punctuation patterns.
        import re
        sentence_count = len(re.findall(r"[.!?](?:\s|$)", result))
        assert sentence_count <= 4, (
            f"summary has {sentence_count} sentences (cap is 3): {result!r}"
        )
        assert len(result) <= 500, (
            f"summary far too long ({len(result)} chars, soft cap 500): {result!r}"
        )

    @_skip_no_model
    @pytest.mark.asyncio
    async def test_warmup_against_real_model(self):
        summarizer = self._make_real_summarizer()
        assert await summarizer.warmup() is True
