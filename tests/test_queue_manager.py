"""Unit tests for QueueManager (Wave 1.D).

All Pipeline interfaces are mocked — these tests do not depend on W1.E/W1.F
having been merged. Covers:
  - Tier transitions across synthetic lag values
  - Hysteresis behaviour at boundaries (no oscillation)
  - ERROR pre-emption (priority_enqueue + terminate)
  - Condensation under YELLOW (2-item batches) and RED (force-summarize, drop low-pri)
  - Hard TTL enforcement (silent drops + skipped count)
  - Backpressure: get_pressure returns the right multiplier per tier
  - Ollama timeout → rule-based merger fires
  - Metric reporting
  - Burst test: 50 events in 2s → tier escalates
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

# Make the project root importable when running pytest from any cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daemon.tts_types import (
    Category,
    PRESSURE_MULTIPLIER,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_ERROR,
    RoutedItem,
    RouterDecision,
    Tier,
)
from daemon.pipeline.ingest_stage import IngestMessage
from daemon.pipeline.queue_manager import (
    DEFAULT_AVG_CHUNK_AUDIO_MS,
    OLLAMA_LATENCY_THRESHOLD_MS,
    QueueManager,
)


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

class _FakePlaybackState:
    """Stand-in for PlaybackState. W1.E will add current_segment_ends_at and
    playback_buffer; we provide both."""
    def __init__(self):
        self.current_segment_ends_at: Optional[float] = None  # ms epoch
        self.playback_buffer: list = []
        self.current_proc = None  # set in pre-emption tests
        self.is_playing = False


class _FakePlaybackStage:
    def __init__(self):
        self.session_states: dict = {}
        self.session_buffers: dict = {}
        self.priority_enqueue = AsyncMock()
        self.flush_buffer = MagicMock(return_value=0)

    def get_session_state(self, session_id: str):
        return self.session_states.get(session_id)

    def get_current_proc(self, session_id: str):
        st = self.session_states.get(session_id)
        return st.current_proc if st else None


class _FakeIngestStage:
    def __init__(self):
        self._pending: dict[str, list[IngestMessage]] = {}

    def get_pending_count(self, session_id: str) -> int:
        return len(self._pending.get(session_id, []))

    def peek_all(self, session_id: str) -> list[IngestMessage]:
        return list(self._pending.get(session_id, []))

    def add(self, session_id: str, msg: IngestMessage):
        self._pending.setdefault(session_id, []).append(msg)


class _FakeOllama:
    """Minimal stand-in for OllamaSummarizer.

    By default condense_batch returns "OLLAMA_CONDENSED:<n>" so tests can
    distinguish from rule-based fallback.
    """
    def __init__(self):
        self.condense_batch = AsyncMock(side_effect=self._default_condense)
        self.summarize = AsyncMock(side_effect=self._default_summarize)
        self.warmup = AsyncMock(return_value=True)
        self.avg_latency_ms = 0.0

    async def _default_condense(self, items):
        return f"OLLAMA_CONDENSED:{len(items)}:" + " | ".join(
            it.decision.content for it in items
        )

    async def _default_summarize(self, content, category, context_hint=""):
        return f"OLLAMA_SUM:{content}"


def _make_qm(
    session_id: str = "s1",
    *,
    pending_count: int = 0,
    in_flight_ms: float = 0.0,
    buffered_ms: float = 0.0,
):
    """Build a QueueManager wired with fake stages, with a session set up
    such that predicted_lag_ms returns approximately the desired value.

    pending_count contributes pending * DEFAULT_AVG_CHUNK_AUDIO_MS.
    in_flight_ms is added by setting current_segment_ends_at.
    buffered_ms is added by inserting one fake segment with that duration.
    """
    playback = _FakePlaybackStage()
    ingest = _FakeIngestStage()
    ollama = _FakeOllama()

    state = _FakePlaybackState()
    if in_flight_ms > 0:
        state.current_segment_ends_at = time.time() * 1000.0 + in_flight_ms
    if buffered_ms > 0:
        seg = types.SimpleNamespace(duration_ms=float(buffered_ms))
        state.playback_buffer.append(seg)
    playback.session_states[session_id] = state

    for i in range(pending_count):
        ingest.add(
            session_id,
            IngestMessage(
                content=f"pending-{i}",
                session_id=session_id,
                priority=PRIORITY_NORMAL,
                request_id=f"pending-{i}",
                ingested_at=time.time(),
            ),
        )

    qm = QueueManager(
        config={},
        ollama_summarizer=ollama,  # type: ignore[arg-type]
        ingest_stage=ingest,        # type: ignore[arg-type]
        playback_stage=playback,    # type: ignore[arg-type]
    )
    return qm, ingest, playback, ollama


def _make_msg(
    content: str = "hello",
    session_id: str = "s1",
    priority: int = PRIORITY_NORMAL,
    request_id: str = "r1",
    age_s: float = 0.0,
) -> IngestMessage:
    return IngestMessage(
        content=content,
        session_id=session_id,
        priority=priority,
        request_id=request_id,
        ingested_at=time.time() - age_s,
    )


def _aiorun(coro):
    """Run a coroutine to completion using a fresh event loop per call.

    Using asyncio.run() per call avoids the Python 3.12+ deprecation of
    asyncio.get_event_loop() outside an active loop and keeps tests isolated.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPredictedLag(unittest.TestCase):
    def test_zero_when_no_state(self):
        qm, _, _, _ = _make_qm("empty")
        self.assertAlmostEqual(qm.predicted_lag_ms("nonexistent"), 0.0, delta=0.1)

    def test_pending_drives_lag(self):
        qm, _, _, _ = _make_qm("s1", pending_count=2)
        # 2 * default_avg = 2 * 3500 = 7000ms
        self.assertAlmostEqual(qm.predicted_lag_ms("s1"), 7000.0, delta=10.0)

    def test_buffered_adds_to_lag(self):
        qm, _, _, _ = _make_qm("s1", pending_count=1, buffered_ms=2000.0)
        # pending=1 * 3500 + buffered 2000 = 5500ms
        self.assertAlmostEqual(qm.predicted_lag_ms("s1"), 5500.0, delta=20.0)

    def test_in_flight_adds_to_lag(self):
        qm, _, _, _ = _make_qm("s1", in_flight_ms=1500.0)
        # pending=0, buffered=0, in_flight ~= 1500
        self.assertAlmostEqual(qm.predicted_lag_ms("s1"), 1500.0, delta=200.0)


class TestTierTransitions(unittest.TestCase):
    def test_green_under_3000(self):
        qm, _, _, _ = _make_qm("s1")  # zero lag
        msg = _make_msg(request_id="m1")
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.GREEN)
        # Pass-through: same content, possibly wrapped in list
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "hello")

    def test_yellow_at_5000(self):
        # 1 pending → 3500ms → YELLOW (>3000, <8000)
        qm, _, _, _ = _make_qm("s1", pending_count=1)
        msg = _make_msg(request_id="m1")
        _aiorun(qm.intercept(msg, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)

    def test_red_at_10000(self):
        # 3 pending → 10500ms → RED (>8000, <15000)
        qm, _, _, _ = _make_qm("s1", pending_count=3)
        msg = _make_msg(request_id="m1", priority=PRIORITY_HIGH)
        _aiorun(qm.intercept(msg, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.RED)

    def test_black_above_15000(self):
        # 5 pending → 17500ms → BLACK
        qm, _, _, _ = _make_qm("s1", pending_count=5)
        msg = _make_msg(request_id="m1")
        _aiorun(qm.intercept(msg, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.BLACK)


class TestHysteresis(unittest.TestCase):
    """Once we escalate to a higher tier, we should NOT drop back to GREEN
    just because lag briefly dips below 3000ms — only when below 2000ms.
    """
    def test_holds_yellow_when_lag_dips_to_2500(self):
        qm, _, _, _ = _make_qm("s1", pending_count=1)  # 3500ms → YELLOW
        msg1 = _make_msg(request_id="m1")
        _aiorun(qm.intercept(msg1, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)

        # Now reduce lag to ~2500ms (in-flight only, no pending)
        qm._ingest._pending["s1"] = []
        qm._playback.session_states["s1"].current_segment_ends_at = (
            time.time() * 1000.0 + 2500.0
        )
        msg2 = _make_msg(request_id="m2")
        _aiorun(qm.intercept(msg2, "s1"))
        # 2500ms is between hysteresis bound (2000ms) and green upper (3000ms)
        # → should HOLD YELLOW, not drop to GREEN.
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)

    def test_drops_to_green_when_lag_below_2000(self):
        qm, _, _, _ = _make_qm("s1", pending_count=1)  # → YELLOW
        msg1 = _make_msg(request_id="m1")
        _aiorun(qm.intercept(msg1, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)

        # Now reduce to 1500ms (well below hysteresis)
        qm._ingest._pending["s1"] = []
        qm._playback.session_states["s1"].current_segment_ends_at = (
            time.time() * 1000.0 + 1500.0
        )
        msg2 = _make_msg(request_id="m2")
        _aiorun(qm.intercept(msg2, "s1"))
        self.assertEqual(qm.get_tier("s1"), Tier.GREEN)

    def test_no_oscillation_at_boundary(self):
        """Repeatedly bouncing across the GREEN/YELLOW boundary at 2500-3500ms
        should not cause the tier to flap to GREEN every time.
        """
        qm, _, _, _ = _make_qm("s1", pending_count=1)  # 3500ms → YELLOW
        for _ in range(5):
            _aiorun(qm.intercept(_make_msg(request_id=f"m-{_}"), "s1"))
            self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)
            # Now jiggle: drop pending, set in_flight to 2500ms
            qm._ingest._pending["s1"] = []
            qm._playback.session_states["s1"].current_segment_ends_at = (
                time.time() * 1000.0 + 2500.0
            )
            _aiorun(qm.intercept(_make_msg(request_id=f"j-{_}"), "s1"))
            self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)  # held by hysteresis
            # Re-add pending to push back into YELLOW
            qm._ingest.add(
                "s1",
                IngestMessage(
                    content="x", session_id="s1", priority=PRIORITY_NORMAL,
                    request_id=f"x-{_}", ingested_at=time.time(),
                ),
            )


class TestErrorPreemption(unittest.TestCase):
    def test_submit_priority_queue_jumps_without_truncating(self):
        # CUTOFF FIX 2026-06-19: ERROR must queue-jump and be counted, but must
        # NOT SIGTERM the active afplay — mid-segment interrupts were slicing
        # real sentences in half (errors are ~42% of decisions). The active
        # segment is left to finish; the ERROR plays next.
        qm, _, playback, _ = _make_qm("s1")
        fake_proc = MagicMock()
        playback.session_states["s1"].current_proc = fake_proc

        decision = RouterDecision(
            should_speak=True,
            category=Category.ERROR,
            content="things broke",
            priority=PRIORITY_ERROR,
            source_event_id="e1",
            classified_at=time.time(),
        )
        item = RoutedItem(decision=decision, session_id="s1")

        result = _aiorun(qm.submit_priority(item))
        self.assertTrue(result)
        # The live segment is NOT killed — this is the whole fix.
        fake_proc.terminate.assert_not_called()
        # ...but the preemption (queue-jump) is still counted.
        m = qm.get_metrics("s1")
        self.assertEqual(m["preemptions_total"], 1)

    def test_submit_priority_no_proc_does_not_raise(self):
        qm, _, _, _ = _make_qm("s1")
        decision = RouterDecision(
            should_speak=True,
            category=Category.ERROR,
            content="boom",
            priority=PRIORITY_ERROR,
            source_event_id="e2",
            classified_at=time.time(),
        )
        item = RoutedItem(decision=decision, session_id="s1")
        # Should not raise even though there's no current_proc.
        self.assertTrue(_aiorun(qm.submit_priority(item)))

    def _err_item(self, eid):
        return RoutedItem(
            decision=RouterDecision(
                should_speak=True, category=Category.ERROR, content="bad",
                priority=PRIORITY_ERROR, source_event_id=eid, classified_at=time.time(),
            ),
            session_id="s1",
        )

    def test_cascade_caps_at_yellow(self):
        """R3/S4: an ERROR cascade COALESCES (caps at YELLOW); it must never
        blindly escalate to BLACK, where the buffer-flush silences pending
        STATUS/INSIGHT wholesale."""
        qm, _, playback, _ = _make_qm("s1")
        _aiorun(qm.submit_priority(self._err_item("e3")))   # GREEN -> YELLOW
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)
        _aiorun(qm.submit_priority(self._err_item("e4")))   # 2nd ERROR -> still YELLOW
        self.assertEqual(qm.get_tier("s1"), Tier.YELLOW)

    def test_cascade_does_not_demote_escalated_session(self):
        """Lag-driven escalation is independent; an ERROR cascade must never
        DEMOTE a genuinely RED/BLACK session."""
        qm, _, playback, _ = _make_qm("s1")
        qm._sessions["s1"].current_tier = Tier.RED
        _aiorun(qm.submit_priority(self._err_item("e5")))
        self.assertEqual(qm.get_tier("s1"), Tier.RED)


class TestYellowCondensation(unittest.TestCase):
    def test_yellow_with_peer_condenses(self):
        qm, ingest, _, ollama = _make_qm("s1", pending_count=1)
        # The pending item is request_id="pending-0" — give it a category.
        qm._sessions["s1"].item_categories["pending-0"] = Category.STATUS
        qm._sessions["s1"].item_categories["m1"] = Category.STATUS

        msg = _make_msg(content="result A", request_id="m1")
        # Make sure pending has matching category.
        result = _aiorun(qm.intercept(msg, "s1"))

        # Should produce a condensed message.
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertTrue(
            result[0].content.startswith("OLLAMA_CONDENSED:")
            or result[0].content.startswith("Multiple updates:")
        )
        ollama.condense_batch.assert_called()
        # condensations counter incremented
        self.assertEqual(qm.get_metrics("s1")["condensations_total"], 1)

    def test_yellow_no_peer_passes_through(self):
        qm, _, _, ollama = _make_qm("s1", pending_count=1)
        # Pending item has different category from the new message
        qm._sessions["s1"].item_categories["pending-0"] = Category.INSIGHT
        qm._sessions["s1"].item_categories["m1"] = Category.STATUS

        msg = _make_msg(content="orphan", request_id="m1")
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNotNone(result)
        self.assertEqual(result[0].content, "orphan")
        ollama.condense_batch.assert_not_called()


class TestRedHandling(unittest.TestCase):
    def test_red_drops_low_pri_status(self):
        qm, _, _, _ = _make_qm("s1", pending_count=3)  # 10500ms → RED
        qm._sessions["s1"].item_categories["m1"] = Category.STATUS

        msg = _make_msg(
            content="trivial", request_id="m1", priority=PRIORITY_LOW
        )
        result = _aiorun(qm.intercept(msg, "s1"))
        # Dropped → returns None
        self.assertIsNone(result)
        # skipped_count incremented
        self.assertEqual(qm._sessions["s1"].skipped_count, 1)
        self.assertEqual(qm._sessions["s1"].drops_total, 1)

    def test_red_summarizes_high_pri(self):
        qm, _, _, ollama = _make_qm("s1", pending_count=3)
        qm._sessions["s1"].item_categories["m1"] = Category.INSIGHT

        msg = _make_msg(
            content="big finding", request_id="m1", priority=PRIORITY_HIGH
        )
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNotNone(result)
        # Single-item summarization via condense_batch
        ollama.condense_batch.assert_called_once()
        # Should produce ollama-condensed content
        self.assertIn("OLLAMA_CONDENSED:1:", result[0].content)

    def test_red_prepends_skipped_preamble(self):
        qm, _, _, _ = _make_qm("s1", pending_count=3)
        # Pre-existing skip count from prior drops
        qm._sessions["s1"].skipped_count = 4
        qm._sessions["s1"].item_categories["m1"] = Category.INSIGHT

        msg = _make_msg(content="finding", request_id="m1", priority=PRIORITY_HIGH)
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNotNone(result)
        self.assertTrue(result[0].content.startswith("Skipping 4 updates."))
        # skip_count reset
        self.assertEqual(qm._sessions["s1"].skipped_count, 0)


class TestBlackHandling(unittest.TestCase):
    def test_black_speaks_catchup_message(self):
        qm, _, playback, _ = _make_qm("s1", pending_count=5)  # 17500ms → BLACK
        msg = _make_msg(content="latest finding", request_id="m1")
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertIn("still working", result[0].content)
        self.assertIn("latest finding", result[0].content)

    def test_black_calls_flush_buffer(self):
        qm, _, playback, _ = _make_qm("s1", pending_count=5)
        msg = _make_msg(content="x", request_id="m1")
        _aiorun(qm.intercept(msg, "s1"))
        playback.flush_buffer.assert_called_once_with("s1", keep_errors=True)


class TestHardTTL(unittest.TestCase):
    def test_old_message_silently_dropped(self):
        qm, _, _, _ = _make_qm("s1")
        # Age 35s exceeds hard TTL of 30s
        msg = _make_msg(content="stale", request_id="m1", age_s=35.0)
        # GREEN tier check shouldn't matter — TTL is checked first
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNone(result)
        self.assertEqual(qm._sessions["s1"].drops_total, 1)
        self.assertEqual(qm._sessions["s1"].skipped_count, 1)

    def test_error_no_ttl(self):
        qm, _, _, _ = _make_qm("s1")
        # Mark as ERROR so TTL is bypassed
        qm._sessions["s1"].item_categories["m1"] = Category.ERROR
        msg = _make_msg(content="boom", request_id="m1", age_s=120.0)
        result = _aiorun(qm.intercept(msg, "s1"))
        # Even though stale, ERROR should not be dropped by TTL
        self.assertIsNotNone(result)


class TestBackpressure(unittest.TestCase):
    def test_pressure_per_tier(self):
        qm, _, _, _ = _make_qm("s1")
        for tier, expected in PRESSURE_MULTIPLIER.items():
            qm._sessions["s1"].current_tier = tier
            self.assertEqual(qm.get_pressure("s1"), expected)

    def test_pressure_default_when_unknown_session(self):
        qm, _, _, _ = _make_qm("s1")
        self.assertEqual(qm.get_pressure("nonexistent"), PRESSURE_MULTIPLIER[Tier.GREEN])


class TestOllamaTimeoutFallback(unittest.TestCase):
    def test_timeout_uses_rule_based_merger(self):
        qm, _, _, ollama = _make_qm("s1", pending_count=1)
        # Drive the timeout-fallback path deterministically: raise TimeoutError
        # directly (the condense wrapper is now clamped to >=4s, so a real sleep
        # would make this test slow and brittle to the clamp value).
        async def _slow(items):
            raise asyncio.TimeoutError()
        ollama.condense_batch.side_effect = _slow

        # Set up YELLOW with a peer.
        qm._sessions["s1"].item_categories["pending-0"] = Category.STATUS
        qm._sessions["s1"].item_categories["m1"] = Category.STATUS

        msg = _make_msg(content="A", request_id="m1")
        result = _aiorun(qm.intercept(msg, "s1"))
        self.assertIsNotNone(result)
        # Rule-based merger output begins with "Multiple updates: " for >1 item
        self.assertTrue(result[0].content.startswith("Multiple updates: "))

    def test_disabled_ollama_skips_call(self):
        qm, _, _, ollama = _make_qm("s1", pending_count=1)
        # Force disabled.
        qm._ollama_disabled_until = time.time() + 60.0
        qm._sessions["s1"].item_categories["pending-0"] = Category.STATUS
        qm._sessions["s1"].item_categories["m1"] = Category.STATUS

        msg = _make_msg(content="A", request_id="m1")
        _aiorun(qm.intercept(msg, "s1"))
        # condense_batch should NOT be called when disabled.
        ollama.condense_batch.assert_not_called()


class TestMetrics(unittest.TestCase):
    def test_per_session_metrics_shape(self):
        qm, _, _, _ = _make_qm("s1")
        m = qm.get_metrics("s1")
        for k in (
            "tier",
            "lag_ms",
            "pending",
            "condensations_total",
            "drops_total",
            "preemptions_total",
            "ollama_avg_ms",
        ):
            self.assertIn(k, m)

    def test_global_metrics_includes_sessions(self):
        qm, _, _, _ = _make_qm("s1")
        # Touch s1
        _aiorun(qm.intercept(_make_msg(request_id="m1"), "s1"))
        g = qm.get_metrics()
        self.assertIn("sessions", g)
        self.assertIn("s1", g["sessions"])


class TestStopHook(unittest.TestCase):
    def test_resets_skip_count_and_categories(self):
        qm, _, _, _ = _make_qm("s1")
        qm._sessions["s1"].skipped_count = 5
        qm._sessions["s1"].item_categories["x"] = Category.STATUS
        _aiorun(qm.on_stop_hook("s1"))
        self.assertEqual(qm._sessions["s1"].skipped_count, 0)
        self.assertEqual(qm._sessions["s1"].item_categories, {})

    def test_unknown_session_no_raise(self):
        qm, _, _, _ = _make_qm("s1")
        _aiorun(qm.on_stop_hook("nonexistent"))  # should silently no-op


class TestRegisterCategoryAndAvgChunk(unittest.TestCase):
    def test_register_category(self):
        qm, _, _, _ = _make_qm("s1")
        qm.register_category("r1", "s1", Category.INSIGHT)
        self.assertEqual(
            qm._sessions["s1"].item_categories["r1"], Category.INSIGHT
        )

    def test_update_avg_chunk_audio_smooths(self):
        qm, _, _, _ = _make_qm("s1")
        before = qm._avg_chunk_audio_ms
        # observed double the default
        qm.update_avg_chunk_audio(7000.0)
        after = qm._avg_chunk_audio_ms
        # EWMA: 0.2 * 7000 + 0.8 * 3500 = 1400 + 2800 = 4200
        self.assertAlmostEqual(after, 4200.0, delta=1.0)
        self.assertGreater(after, before)


class TestBurst(unittest.TestCase):
    """Simulate 50 events arriving in 2s; assert tier escalates GREEN→YELLOW→RED.
    """
    def test_burst_escalates_tiers(self):
        qm, ingest, _, _ = _make_qm("s1")
        seen_tiers: list[Tier] = []

        async def burst():
            # Fire 50 messages over a simulated window. Backlog grows because
            # the orchestrator (in real life) would add to ingest BEFORE
            # consume — but here we only add the *next* messages to mimic
            # buildup. We intercept the current msg first, then add a few
            # more peers to the pending list to simulate queue depth growth.
            for i in range(50):
                msg = IngestMessage(
                    content=f"burst-{i}",
                    session_id="s1",
                    priority=PRIORITY_NORMAL,
                    request_id=f"b-{i}",
                    ingested_at=time.time(),
                )
                # Intercept FIRST (the current message has been consumed).
                await qm.intercept(msg, "s1")
                seen_tiers.append(qm.get_tier("s1"))
                # Then add the NEXT message to the pending backlog so the
                # NEXT iteration sees it. After 4 iterations, backlog ≈ 4.
                ingest.add(
                    "s1",
                    IngestMessage(
                        content=f"buf-{i}",
                        session_id="s1",
                        priority=PRIORITY_NORMAL,
                        request_id=f"buf-{i}",
                        ingested_at=time.time(),
                    ),
                )
            return seen_tiers

        result = _aiorun(burst())
        # We should have seen at least one GREEN early on, escalating to YELLOW
        # and then RED (and likely BLACK at the tail).
        self.assertEqual(result[0], Tier.GREEN, f"first tier was {result[0]}")
        self.assertIn(Tier.YELLOW, result)
        self.assertIn(Tier.RED, result)
        # And the final tier should be at least RED.
        self.assertIn(result[-1], (Tier.RED, Tier.BLACK))


if __name__ == "__main__":
    unittest.main()
