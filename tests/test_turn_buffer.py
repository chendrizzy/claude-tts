"""Wave 2.C unit tests for TurnBuffer + ContentRouter integration.

Covers:
* TurnBuffer.add accumulates without flushing immediately
* 800ms (configurable) idle timeout triggers flush
* Manual flush(reason="stop") drains immediately and cancels pending timer
* Multiple add() calls extend (re-arm) the timer
* flush is idempotent on empty buffer
* pending_count and oldest_age_ms behave as documented
* ContentRouter.turn_buffer_for raises if callback not wired
* ContentRouter.turn_buffer_for returns the same instance for the same session_id
* ContentRouter.turn_buffer_for returns distinct instances for distinct session_ids
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import List

import pytest

# Add project root to sys.path so `daemon.*` imports resolve regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.content_router import ContentRouter, TurnBuffer  # noqa: E402
from daemon.tts_types import (  # noqa: E402
    Category,
    PRIORITY_NORMAL,
    RoutedItem,
    RouterDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FlushRecorder:
    """Async callable mock recording every flush invocation.

    Each call appends ``(timestamp, [items])`` to ``self.calls`` so tests can
    assert both *what* was flushed and *when* (relative ordering).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[float, list[RoutedItem]]] = []

    async def __call__(self, items: list[RoutedItem]) -> None:
        # Take a defensive copy of the snapshot — TurnBuffer hands us its
        # internal list reference; we don't want later mutations changing
        # what tests observe.
        self.calls.append((time.time(), list(items)))


def _make_item(content: str = "x", session_id: str = "s1") -> RoutedItem:
    """Build a minimal, valid RoutedItem for buffer tests."""
    decision = RouterDecision(
        should_speak=True,
        category=Category.STATUS,
        content=content,
        priority=PRIORITY_NORMAL,
        source_event_id=f"ev-{content}",
        classified_at=time.time(),
        needs_summarization=False,
        context_hint="test",
        raw_excerpt=content[:80],
    )
    return RoutedItem(decision=decision, session_id=session_id)


class MockOllamaSummarizer:
    """No-op summarizer — TurnBuffer doesn't use it but ContentRouter ctor needs one."""

    async def summarize(self, content: str, category: Category, context_hint: str = "") -> str:
        return content


# ---------------------------------------------------------------------------
# TurnBuffer.add behaviour
# ---------------------------------------------------------------------------

class TestAddAccumulates:
    @pytest.mark.asyncio
    async def test_add_does_not_flush_synchronously(self) -> None:
        """add() must buffer; no callback invocation until idle timer or manual flush."""
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)

        await buf.add(_make_item("a"))
        await buf.add(_make_item("b"))
        await buf.add(_make_item("c"))

        # Yield once so any synchronous tasks would have a chance to fire.
        await asyncio.sleep(0)

        assert recorder.calls == []
        assert buf.pending_count == 3

    @pytest.mark.asyncio
    async def test_add_none_is_a_noop(self) -> None:
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)
        await buf.add(None)  # type: ignore[arg-type]
        assert buf.pending_count == 0


# ---------------------------------------------------------------------------
# Idle-timer flush
# ---------------------------------------------------------------------------

class TestIdleFlush:
    @pytest.mark.asyncio
    async def test_idle_window_triggers_flush(self) -> None:
        """After idle_window_ms with no add(), buffer flushes through callback."""
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=50)

        await buf.add(_make_item("only-item"))
        # Sleep just over the idle window to give the timer time to fire and
        # the resulting create_task to run flush_callback.
        await asyncio.sleep(0.12)

        assert len(recorder.calls) == 1
        _, items = recorder.calls[0]
        assert [it.decision.content for it in items] == ["only-item"]
        # Buffer is drained after flush.
        assert buf.pending_count == 0

    @pytest.mark.asyncio
    async def test_re_arm_extends_timer_on_each_add(self) -> None:
        """Each add() restarts the idle clock — burst of adds → single flush at end."""
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=80)

        await buf.add(_make_item("a"))
        await asyncio.sleep(0.04)  # < idle window
        await buf.add(_make_item("b"))
        await asyncio.sleep(0.04)  # < idle window since last add
        await buf.add(_make_item("c"))

        # Now wait the full window — should flush all three together.
        await asyncio.sleep(0.15)

        assert len(recorder.calls) == 1, (
            f"expected one batch flush, got {len(recorder.calls)}"
        )
        _, items = recorder.calls[0]
        assert [it.decision.content for it in items] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Manual flush
# ---------------------------------------------------------------------------

class TestManualFlush:
    @pytest.mark.asyncio
    async def test_manual_flush_is_immediate_and_cancels_idle_timer(self) -> None:
        """flush(reason="stop") drains synchronously and prevents the idle flush
        from also firing afterward.
        """
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=80)

        await buf.add(_make_item("a"))
        await buf.add(_make_item("b"))

        # Manual flush before the idle timer would have fired.
        await buf.flush(reason="stop")

        # Wait long enough that an un-cancelled timer would have re-fired.
        await asyncio.sleep(0.15)

        # Exactly one flush call — no double-flush from the idle path.
        assert len(recorder.calls) == 1
        _, items = recorder.calls[0]
        assert [it.decision.content for it in items] == ["a", "b"]
        assert buf.pending_count == 0

    @pytest.mark.asyncio
    async def test_flush_idempotent_on_empty(self) -> None:
        """flush() with nothing buffered must not invoke the callback at all."""
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)

        await buf.flush(reason="manual")
        await buf.flush(reason="manual")
        await buf.flush(reason="stop")

        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_flush_after_drain_is_noop(self) -> None:
        """Flush, then flush again with no fresh adds — second is a no-op."""
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)

        await buf.add(_make_item("a"))
        await buf.flush(reason="stop")
        await buf.flush(reason="stop")

        assert len(recorder.calls) == 1

    @pytest.mark.asyncio
    async def test_flush_callback_exceptions_do_not_propagate(self) -> None:
        """If flush_callback raises, flush() must not raise — it logs and returns."""

        async def boom(_: list[RoutedItem]) -> None:
            raise RuntimeError("simulated downstream failure")

        buf = TurnBuffer("s1", boom, idle_window_ms=10_000)
        await buf.add(_make_item("a"))
        # Should not raise.
        await buf.flush(reason="stop")
        # Buffer drained even though callback failed.
        assert buf.pending_count == 0


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    @pytest.mark.asyncio
    async def test_pending_count_tracks_adds(self) -> None:
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)
        assert buf.pending_count == 0
        await buf.add(_make_item("a"))
        assert buf.pending_count == 1
        await buf.add(_make_item("b"))
        assert buf.pending_count == 2
        await buf.flush()
        assert buf.pending_count == 0

    @pytest.mark.asyncio
    async def test_oldest_age_ms_zero_when_empty(self) -> None:
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)
        assert buf.oldest_age_ms == 0.0

    @pytest.mark.asyncio
    async def test_oldest_age_ms_grows_with_time(self) -> None:
        recorder = FlushRecorder()
        buf = TurnBuffer("s1", recorder, idle_window_ms=10_000)
        await buf.add(_make_item("oldest"))
        await asyncio.sleep(0.05)
        await buf.add(_make_item("newer"))

        # oldest_age should reflect ~50ms+ at this point.
        age = buf.oldest_age_ms
        assert age >= 40.0, f"expected >=40ms; got {age}ms"
        # And tracks the OLDEST item (the first one added), not the newest.
        first = buf._pending[0]  # noqa: SLF001 — internal detail under test
        assert first.decision.content == "oldest"


# ---------------------------------------------------------------------------
# ContentRouter.turn_buffer_for integration
# ---------------------------------------------------------------------------

class TestContentRouterIntegration:
    def _make_router(self) -> ContentRouter:
        return ContentRouter(
            config={}, ollama_summarizer=MockOllamaSummarizer()
        )

    def test_turn_buffer_for_raises_without_callback(self) -> None:
        """Explicit fail-safe: forgetting to wire the callback must error,
        not silently drop batches.
        """
        router = self._make_router()
        with pytest.raises(RuntimeError, match="TurnBuffer flush callback not wired"):
            router.turn_buffer_for("s1")

    @pytest.mark.asyncio
    async def test_turn_buffer_for_lazy_caches_per_session(self) -> None:
        """Same session_id → same TurnBuffer instance; distinct session_ids → distinct."""
        recorder = FlushRecorder()
        router = self._make_router()
        router.set_turn_buffer_callback(recorder)

        buf_a1 = router.turn_buffer_for("a")
        buf_a2 = router.turn_buffer_for("a")
        buf_b = router.turn_buffer_for("b")

        assert buf_a1 is buf_a2, "same session_id must return the same buffer"
        assert buf_a1 is not buf_b, "different session_ids must get distinct buffers"
        assert buf_a1.session_id == "a"
        assert buf_b.session_id == "b"

    @pytest.mark.asyncio
    async def test_buffer_uses_configured_idle_window(self) -> None:
        """Routing config flows into TurnBuffer.idle_window_ms."""
        recorder = FlushRecorder()
        router = ContentRouter(
            config={"routing": {"turn_buffer_idle_ms": 222}},
            ollama_summarizer=MockOllamaSummarizer(),
        )
        router.set_turn_buffer_callback(recorder)
        buf = router.turn_buffer_for("s1")
        assert buf.idle_window_ms == 222

    @pytest.mark.asyncio
    async def test_buffer_default_idle_window_is_800ms(self) -> None:
        """Spec: default idle window is 800ms when no config provided."""
        recorder = FlushRecorder()
        router = self._make_router()
        router.set_turn_buffer_callback(recorder)
        buf = router.turn_buffer_for("s1")
        assert buf.idle_window_ms == 800

    @pytest.mark.asyncio
    async def test_end_to_end_add_then_flush(self) -> None:
        """Smoke: route through ContentRouter.turn_buffer_for + add + manual flush."""
        recorder = FlushRecorder()
        router = self._make_router()
        router.set_turn_buffer_callback(recorder)

        buf = router.turn_buffer_for("session-xyz")
        await buf.add(_make_item("hello", session_id="session-xyz"))
        await buf.add(_make_item("world", session_id="session-xyz"))
        await buf.flush(reason="stop")

        assert len(recorder.calls) == 1
        _, items = recorder.calls[0]
        assert [it.decision.content for it in items] == ["hello", "world"]
        assert all(it.session_id == "session-xyz" for it in items)
