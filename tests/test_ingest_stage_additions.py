"""Tests for W1.F additions to IngestStage: peek_all() and get_pending_count().

These methods are called by QueueManager (W1.D) to inspect upcoming items for
condensation candidates without consuming them.

Run from project root:
    python3 -m pytest tests/test_ingest_stage_additions.py -v
or:
    python3 tests/test_ingest_stage_additions.py
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

# Allow running directly from project root or tests/ dir.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from daemon.pipeline.ingest_stage import IngestMessage, IngestStage  # noqa: E402


def _msg(session_id: str, content: str, request_id: str, priority: int = 5) -> IngestMessage:
    return IngestMessage(
        content=content,
        session_id=session_id,
        priority=priority,
        request_id=request_id,
        ingested_at=time.time(),
        source="test",
    )


class PeekAllTests(unittest.IsolatedAsyncioTestCase):
    """peek_all snapshots pending messages without consuming them."""

    async def test_peek_empty_for_unknown_session(self) -> None:
        stage = IngestStage()
        result = await stage.peek_all("never-seen")
        self.assertEqual(result, [])

    async def test_peek_returns_messages_in_order(self) -> None:
        stage = IngestStage()
        sid = "sess-A"
        msgs = [_msg(sid, f"content-{i}", f"req-{i}") for i in range(3)]
        for m in msgs:
            ok = await stage.ingest(m)
            self.assertTrue(ok)

        snapshot = await stage.peek_all(sid)
        self.assertEqual(len(snapshot), 3)
        self.assertEqual([m.request_id for m in snapshot], ["req-0", "req-1", "req-2"])
        self.assertEqual([m.content for m in snapshot], ["content-0", "content-1", "content-2"])

    async def test_peek_does_not_consume(self) -> None:
        """The critical invariant: peek must not lose messages."""
        stage = IngestStage()
        sid = "sess-B"
        msgs = [_msg(sid, f"c{i}", f"r{i}") for i in range(3)]
        for m in msgs:
            await stage.ingest(m)

        # Peek twice — should get identical snapshots
        snap1 = await stage.peek_all(sid)
        snap2 = await stage.peek_all(sid)
        self.assertEqual([m.request_id for m in snap1], ["r0", "r1", "r2"])
        self.assertEqual([m.request_id for m in snap2], ["r0", "r1", "r2"])

        # Now consume — must still receive all 3 in order
        consumed = []
        for _ in range(3):
            m = await asyncio.wait_for(stage.consume(sid, timeout=1.0), timeout=2.0)
            self.assertIsNotNone(m)
            consumed.append(m.request_id)
        self.assertEqual(consumed, ["r0", "r1", "r2"])

        # Queue is now empty; peek returns []
        self.assertEqual(await stage.peek_all(sid), [])

    async def test_peek_does_not_affect_other_sessions(self) -> None:
        stage = IngestStage()
        await stage.ingest(_msg("sess-X", "x1", "x-r1"))
        await stage.ingest(_msg("sess-Y", "y1", "y-r1"))
        await stage.ingest(_msg("sess-Y", "y2", "y-r2"))

        x_snap = await stage.peek_all("sess-X")
        y_snap = await stage.peek_all("sess-Y")
        self.assertEqual([m.request_id for m in x_snap], ["x-r1"])
        self.assertEqual([m.request_id for m in y_snap], ["y-r1", "y-r2"])

    async def test_peek_after_partial_consume(self) -> None:
        stage = IngestStage()
        sid = "sess-partial"
        for i in range(4):
            await stage.ingest(_msg(sid, f"c{i}", f"r{i}"))

        # Consume first one
        first = await asyncio.wait_for(stage.consume(sid, timeout=1.0), timeout=2.0)
        self.assertEqual(first.request_id, "r0")

        snap = await stage.peek_all(sid)
        self.assertEqual([m.request_id for m in snap], ["r1", "r2", "r3"])


class PendingCountTests(unittest.IsolatedAsyncioTestCase):
    """get_pending_count is sync and cheap."""

    async def test_count_zero_for_unknown_session(self) -> None:
        stage = IngestStage()
        self.assertEqual(stage.get_pending_count("nope"), 0)

    async def test_count_matches_ingested(self) -> None:
        stage = IngestStage()
        sid = "sess-count"
        self.assertEqual(stage.get_pending_count(sid), 0)

        await stage.ingest(_msg(sid, "a", "r1"))
        self.assertEqual(stage.get_pending_count(sid), 1)

        await stage.ingest(_msg(sid, "b", "r2"))
        await stage.ingest(_msg(sid, "c", "r3"))
        self.assertEqual(stage.get_pending_count(sid), 3)

    async def test_count_unchanged_by_peek(self) -> None:
        stage = IngestStage()
        sid = "sess-cnt-peek"
        for i in range(3):
            await stage.ingest(_msg(sid, f"c{i}", f"r{i}"))
        self.assertEqual(stage.get_pending_count(sid), 3)
        await stage.peek_all(sid)
        self.assertEqual(stage.get_pending_count(sid), 3)

    async def test_count_decrements_on_consume(self) -> None:
        stage = IngestStage()
        sid = "sess-cnt-consume"
        await stage.ingest(_msg(sid, "a", "r1"))
        await stage.ingest(_msg(sid, "b", "r2"))
        self.assertEqual(stage.get_pending_count(sid), 2)
        await asyncio.wait_for(stage.consume(sid, timeout=1.0), timeout=2.0)
        self.assertEqual(stage.get_pending_count(sid), 1)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """cleanup_session must release the peek lock too."""

    async def test_cleanup_removes_peek_lock(self) -> None:
        stage = IngestStage()
        sid = "sess-cleanup"
        await stage.ingest(_msg(sid, "a", "r1"))
        # Force lock creation by peeking
        await stage.peek_all(sid)
        self.assertIn(sid, stage._peek_locks)

        await stage.cleanup_session(sid)
        self.assertNotIn(sid, stage._peek_locks)
        self.assertNotIn(sid, stage.session_queues)
        # Subsequent count is 0 again
        self.assertEqual(stage.get_pending_count(sid), 0)


class ConcurrentPeekConsumeTests(unittest.IsolatedAsyncioTestCase):
    """peek_all and consume must serialize cleanly under contention."""

    async def test_concurrent_peek_does_not_lose_messages(self) -> None:
        stage = IngestStage()
        sid = "sess-race"
        for i in range(5):
            await stage.ingest(_msg(sid, f"c{i}", f"r{i}"))

        # Run several peeks concurrently with one consumer
        peek_results: list[list[IngestMessage]] = []

        async def peeker() -> None:
            peek_results.append(await stage.peek_all(sid))

        async def consumer() -> list[str]:
            got = []
            for _ in range(5):
                m = await asyncio.wait_for(stage.consume(sid, timeout=1.0), timeout=2.0)
                got.append(m.request_id)
            return got

        # Interleave 3 peeks and 1 consumer
        results = await asyncio.gather(
            peeker(), peeker(), consumer(), peeker(),
        )
        consumed_ids = results[2]
        self.assertEqual(consumed_ids, ["r0", "r1", "r2", "r3", "r4"])
        # Each peek snapshot must be a contiguous prefix of the original order
        # (it might be partial because consumer interleaved, but never reordered)
        for snap in peek_results:
            ids = [m.request_id for m in snap]
            # ids should be sorted ascending by their numeric suffix and contiguous
            nums = [int(rid.lstrip("r")) for rid in ids]
            self.assertEqual(nums, sorted(nums), f"snapshot not in order: {ids}")
            if nums:
                # Contiguous from some starting point
                self.assertEqual(nums, list(range(nums[0], nums[0] + len(nums))),
                                 f"snapshot not contiguous: {ids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
