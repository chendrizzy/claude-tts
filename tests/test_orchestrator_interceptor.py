"""Tests for the QueueManager interceptor wiring in TTSPipelineOrchestrator (W2.B).

Verifies that `_process_session` calls `queue_manager.intercept` between
`ingest.consume` and `process.process`, and that the four documented return
shapes are honored:
    - None        -> message dropped (process not called)
    - []          -> condensed-to-empty (process not called)
    - [msg]       -> passthrough (process called once)
    - [m1, m2]    -> expansion (process called once per replacement)

Also verifies backward compatibility: when `queue_manager` is None, the
orchestrator behaves exactly like the legacy single-message path.

All pipeline stages (Ingest, Process, Generate, Playback) AND the
QueueManager itself are mocked. No subprocess / Edge-TTS / afplay touched.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

# Make project root importable when running pytest from any cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daemon.pipeline.ingest_stage import IngestMessage
from daemon.pipeline.orchestrator import TTSPipelineOrchestrator


# ---------------------------------------------------------------------------
# Shared fixtures: build an orchestrator with all stages mocked
# ---------------------------------------------------------------------------

def _make_msg(content: str = "hello", request_id: str = "r1",
              session_id: str = "s1") -> IngestMessage:
    return IngestMessage(
        content=content,
        session_id=session_id,
        priority=5,
        request_id=request_id,
        ingested_at=time.time(),
    )


async def _async_iter(items):
    """Helper: turn a list into an async iterator (matches GenerateStage)."""
    for it in items:
        yield it


def _build_orchestrator(*, queue_manager=None):
    """Construct an orchestrator and replace each stage with a mock.

    Returns the orchestrator and a dict of the mocks for assertions.
    """
    orch = TTSPipelineOrchestrator(
        workers_per_session=1,
        chunk_size=150,
        buffer_size=1,
        cache_dir="/tmp/tts_test_cache",
        voice="en-US-AriaNeural",
        queue_manager=queue_manager,
    )

    # Mock IngestStage: .start, .stop, .consume, .cleanup_session
    orch.ingest.start = AsyncMock()
    orch.ingest.stop = AsyncMock()
    orch.ingest.cleanup_session = AsyncMock()
    # consume() defaults to None; tests override with side_effect.
    orch.ingest.consume = AsyncMock(return_value=None)

    # Mock ProcessStage: .process returns a sentinel "processed" marker.
    orch.process.process = AsyncMock(side_effect=lambda m: ("processed", m))

    # Mock GenerateStage: .generate yields a single fake segment per call.
    fake_segment = MagicMock(name="AudioSegment")
    orch.generate.generate = MagicMock(
        side_effect=lambda processed, voice: _async_iter([fake_segment])
    )
    orch.generate.cleanup_session = AsyncMock()

    # Mock PlaybackStage: .start_session, .stop_session, .enqueue_segment.
    orch.playback.start_session = AsyncMock()
    orch.playback.stop_session = AsyncMock()
    orch.playback.enqueue_segment = AsyncMock()
    orch.playback.session_buffers = {}  # so stop() doesn't iterate real keys

    mocks = {
        "ingest_consume": orch.ingest.consume,
        "process_process": orch.process.process,
        "generate_generate": orch.generate.generate,
        "playback_enqueue": orch.playback.enqueue_segment,
    }
    return orch, mocks


async def _run_one_iteration(orch: TTSPipelineOrchestrator, session_id: str,
                             messages: list[Optional[IngestMessage]]):
    """Drive `_process_session` for exactly len(messages) iterations.

    Each entry in `messages` is what `ingest.consume` returns on that
    iteration; after the list is exhausted we set `_running = False` and
    return None so the loop exits cleanly.
    """
    consume_results = list(messages) + [None]  # final None triggers exit
    call_index = {"i": 0}

    async def _consume(sid, timeout=1.0):
        i = call_index["i"]
        call_index["i"] += 1
        if i >= len(consume_results):
            orch._running = False
            return None
        result = consume_results[i]
        if result is None:
            # Exhausted real messages — flip the flag so loop exits.
            orch._running = False
        return result

    orch.ingest.consume.side_effect = _consume
    orch._running = True
    await orch._process_session(session_id, voice=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInterceptorWiring(unittest.TestCase):
    """The intercept call must be wired between consume and process."""

    def test_intercept_called_between_consume_and_process(self):
        """Verify call ordering: consume -> intercept -> process."""
        qm = MagicMock()
        msg = _make_msg("hello world", "r1")
        # Passthrough so the message reaches process.
        qm.intercept = AsyncMock(return_value=[msg])

        orch, mocks = _build_orchestrator(queue_manager=qm)

        call_order: list[str] = []

        async def _consume(sid, timeout=1.0):
            call_order.append("consume")
            if len(call_order) > 1:
                # Already called once — exit loop.
                orch._running = False
                return None
            return msg

        async def _intercept(message, session_id):
            call_order.append("intercept")
            return [message]

        async def _process(message):
            call_order.append("process")
            return ("processed", message)

        orch.ingest.consume.side_effect = _consume
        qm.intercept.side_effect = _intercept
        orch.process.process.side_effect = _process

        async def _go():
            orch._running = True
            await orch._process_session("s1", voice=None)

        asyncio.run(_go())

        # First three calls must be in this exact order.
        self.assertEqual(call_order[:3], ["consume", "intercept", "process"])

    def test_intercept_called_with_message_and_session_id(self):
        """Verify intercept receives the consumed message + session_id."""
        qm = MagicMock()
        msg = _make_msg("payload", "req-42", session_id="my-session")
        qm.intercept = AsyncMock(return_value=[msg])

        orch, _ = _build_orchestrator(queue_manager=qm)

        asyncio.run(_run_one_iteration(orch, "my-session", [msg]))

        # intercept should have been invoked once with (message, session_id).
        self.assertEqual(qm.intercept.call_count, 1)
        args, kwargs = qm.intercept.call_args
        # Accept positional or keyword form.
        if args:
            passed_msg, passed_sid = args[0], args[1]
        else:
            passed_msg = kwargs["message"]
            passed_sid = kwargs["session_id"]
        self.assertIs(passed_msg, msg)
        self.assertEqual(passed_sid, "my-session")


class TestNoneReturnDropsMessage(unittest.TestCase):
    """`intercept(...) -> None` must drop the message (process not called)."""

    def test_none_return_skips_process(self):
        qm = MagicMock()
        qm.intercept = AsyncMock(return_value=None)

        orch, mocks = _build_orchestrator(queue_manager=qm)
        msg = _make_msg("dropped", "r1")
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        # intercept fired once, process never called.
        self.assertEqual(qm.intercept.call_count, 1)
        self.assertEqual(mocks["process_process"].call_count, 0)
        self.assertEqual(mocks["generate_generate"].call_count, 0)
        self.assertEqual(mocks["playback_enqueue"].call_count, 0)

    def test_empty_list_return_skips_process(self):
        """`intercept -> []` (condensed-to-empty) must also drop."""
        qm = MagicMock()
        qm.intercept = AsyncMock(return_value=[])

        orch, mocks = _build_orchestrator(queue_manager=qm)
        msg = _make_msg("condensed-away", "r1")
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        self.assertEqual(qm.intercept.call_count, 1)
        self.assertEqual(mocks["process_process"].call_count, 0)
        self.assertEqual(mocks["generate_generate"].call_count, 0)


class TestPassthroughBehavior(unittest.TestCase):
    """`intercept(...) -> [msg]` must call process exactly once with msg."""

    def test_passthrough_calls_process_once(self):
        qm = MagicMock()
        msg = _make_msg("hello", "r1")
        qm.intercept = AsyncMock(return_value=[msg])

        orch, mocks = _build_orchestrator(queue_manager=qm)
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        self.assertEqual(qm.intercept.call_count, 1)
        self.assertEqual(mocks["process_process"].call_count, 1)
        # process was called with the same message returned by intercept.
        called_arg = mocks["process_process"].call_args.args[0]
        self.assertIs(called_arg, msg)
        # generate + playback fired once for that single message.
        self.assertEqual(mocks["generate_generate"].call_count, 1)
        self.assertEqual(mocks["playback_enqueue"].call_count, 1)


class TestExpansionBehavior(unittest.TestCase):
    """`intercept(...) -> [m1, m2]` must call process for each replacement."""

    def test_two_message_expansion_calls_process_twice(self):
        qm = MagicMock()
        original = _make_msg("orig", "r-orig")
        m1 = _make_msg("expanded-1", "r-1")
        m2 = _make_msg("expanded-2", "r-2")
        qm.intercept = AsyncMock(return_value=[m1, m2])

        orch, mocks = _build_orchestrator(queue_manager=qm)
        asyncio.run(_run_one_iteration(orch, "s1", [original]))

        self.assertEqual(qm.intercept.call_count, 1)
        # process should have been called twice — once per replacement.
        self.assertEqual(mocks["process_process"].call_count, 2)
        actual = [c.args[0] for c in mocks["process_process"].call_args_list]
        self.assertIs(actual[0], m1)
        self.assertIs(actual[1], m2)
        # generate + playback both fire twice as well.
        self.assertEqual(mocks["generate_generate"].call_count, 2)
        self.assertEqual(mocks["playback_enqueue"].call_count, 2)

    def test_three_message_expansion(self):
        """Sanity: arbitrary list length expands correctly."""
        qm = MagicMock()
        original = _make_msg("orig", "r-orig")
        replacements = [
            _make_msg(f"r{i}", f"req-{i}") for i in range(3)
        ]
        qm.intercept = AsyncMock(return_value=replacements)

        orch, mocks = _build_orchestrator(queue_manager=qm)
        asyncio.run(_run_one_iteration(orch, "s1", [original]))

        self.assertEqual(mocks["process_process"].call_count, 3)


class TestBackwardCompatibility(unittest.TestCase):
    """When QueueManager is None, behavior is exactly the legacy path."""

    def test_no_queue_manager_passes_through(self):
        """With queue_manager=None, original message goes straight to process."""
        orch, mocks = _build_orchestrator(queue_manager=None)
        self.assertIsNone(orch.queue_manager)

        msg = _make_msg("legacy", "r1")
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        # process called once with original message.
        self.assertEqual(mocks["process_process"].call_count, 1)
        called_arg = mocks["process_process"].call_args.args[0]
        self.assertIs(called_arg, msg)
        self.assertEqual(mocks["generate_generate"].call_count, 1)
        self.assertEqual(mocks["playback_enqueue"].call_count, 1)

    def test_set_queue_manager_late_bind(self):
        """`set_queue_manager` should switch the loop to interceptor mode."""
        orch, mocks = _build_orchestrator(queue_manager=None)
        self.assertIsNone(orch.queue_manager)

        # Late-bind a QueueManager that drops everything.
        qm = MagicMock()
        qm.intercept = AsyncMock(return_value=None)
        orch.set_queue_manager(qm)
        self.assertIs(orch.queue_manager, qm)

        msg = _make_msg("post-bind", "r1")
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        # intercept fired (because we late-bound it); process did NOT.
        self.assertEqual(qm.intercept.call_count, 1)
        self.assertEqual(mocks["process_process"].call_count, 0)

    def test_set_queue_manager_to_none_restores_legacy(self):
        """Passing None to setter reverts to legacy path."""
        qm = MagicMock()
        qm.intercept = AsyncMock(return_value=[])  # would drop everything

        orch, mocks = _build_orchestrator(queue_manager=qm)
        self.assertIs(orch.queue_manager, qm)

        # Unbind.
        orch.set_queue_manager(None)
        self.assertIsNone(orch.queue_manager)

        msg = _make_msg("legacy-again", "r1")
        asyncio.run(_run_one_iteration(orch, "s1", [msg]))

        # qm.intercept should NOT be called; process should be called once.
        self.assertEqual(qm.intercept.call_count, 0)
        self.assertEqual(mocks["process_process"].call_count, 1)


class TestPipelineAdapterPropagation(unittest.TestCase):
    """PipelineAdapter must thread the QueueManager through to the orchestrator."""

    def test_adapter_constructor_accepts_queue_manager(self):
        from daemon.pipeline.adapter import PipelineAdapter

        qm = MagicMock()
        adapter = PipelineAdapter(queue_manager=qm)
        # Stashed for later forwarding.
        self.assertIs(adapter._queue_manager, qm)

    def test_adapter_set_queue_manager_late_bind(self):
        """Setting QM after construction stashes it; if orchestrator is up,
        it should also be forwarded immediately.
        """
        from daemon.pipeline.adapter import PipelineAdapter

        adapter = PipelineAdapter()
        self.assertIsNone(adapter._queue_manager)

        qm = MagicMock()
        adapter.set_queue_manager(qm)
        self.assertIs(adapter._queue_manager, qm)

        # If we now manually set an orchestrator, the next set_queue_manager
        # call should propagate to it.
        fake_orch = MagicMock()
        adapter._orchestrator = fake_orch
        qm2 = MagicMock()
        adapter.set_queue_manager(qm2)
        fake_orch.set_queue_manager.assert_called_once_with(qm2)


if __name__ == "__main__":
    unittest.main()
