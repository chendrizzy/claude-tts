"""
Tests for DAEMON-03: asyncio.to_thread wrap of legacy global audio lock.

These five tests implement the M0 anti-pattern guard per VERIFICATION.md B-2
and 01-PLAN-03-fcntl-wrap.md.

Tests 3, 4, 5 are the critical M0 guards — they MUST FAIL on the original
code (plain `with self._legacy_global_audio_lock()`) and PASS after the fix
(`asyncio.to_thread(self._acquire_legacy_global_lock)` + try/finally).

Run with: python3 -m pytest tests/test_playback_stage_legacy_lock.py -v
"""
import ast
import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.pipeline.playback_stage import PlaybackStage, PlaybackState  # noqa: E402

PLAYBACK = Path(__file__).resolve().parent.parent / "daemon" / "pipeline" / "playback_stage.py"


class TestLegacyLockHelpers(unittest.IsolatedAsyncioTestCase):
    """Test 1: acquire/release helpers exist and are thread-compatible."""

    async def test_acquire_helpers_exist_and_are_thread_compatible(self):
        """Both _acquire_legacy_global_lock and _release_legacy_global_lock must exist
        as callable attributes that can be invoked via asyncio.to_thread."""
        stage = PlaybackStage()

        self.assertTrue(
            hasattr(stage, "_acquire_legacy_global_lock"),
            "_acquire_legacy_global_lock method missing from PlaybackStage",
        )
        self.assertTrue(
            hasattr(stage, "_release_legacy_global_lock"),
            "_release_legacy_global_lock method missing from PlaybackStage",
        )
        self.assertTrue(
            callable(stage._acquire_legacy_global_lock),
            "_acquire_legacy_global_lock is not callable",
        )
        self.assertTrue(
            callable(stage._release_legacy_global_lock),
            "_release_legacy_global_lock is not callable",
        )

        # Exercise both via to_thread to confirm they are compatible
        lock_file = await asyncio.to_thread(stage._acquire_legacy_global_lock)
        try:
            pass
        finally:
            await asyncio.to_thread(stage._release_legacy_global_lock, lock_file)


class TestEventLoopNotStalled(unittest.IsolatedAsyncioTestCase):
    """Test 2: event loop is not stalled during lock acquisition (tightened threshold)."""

    async def test_event_loop_is_not_stalled_during_lock_acquisition(self):
        """Acquire the fcntl lock via to_thread while running a heartbeat concurrently.
        The heartbeat must tick >= 20 times (out of 25 attempts at 0.02s each).

        Without the to_thread wrap, time.sleep(0.05) blocks the event loop and
        ticks would be ~0. With the fix, acquire runs in a worker thread, the
        event loop stays free, and the heartbeat accumulates ~25 ticks.

        Design: acquire lock in worker thread, release immediately (no contention),
        then let heartbeat run to completion alongside the competitor.
        The key assertion is on heartbeat ticks, proving the loop was unblocked
        during the acquire busy-wait."""
        stage = PlaybackStage()

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(25):
                await asyncio.sleep(0.02)
                ticks += 1

        async def competitor():
            # acquire then immediately release — tests that to_thread doesn't
            # block the event loop during the busy-wait spin
            lf = await asyncio.to_thread(stage._acquire_legacy_global_lock)
            await asyncio.to_thread(stage._release_legacy_global_lock, lf)

        beat = asyncio.create_task(heartbeat())
        comp = asyncio.create_task(competitor())

        # Both tasks run concurrently; competitor's busy-wait must not stall heartbeat
        await asyncio.wait_for(asyncio.gather(beat, comp), timeout=3.0)

        self.assertGreaterEqual(
            ticks, 20,
            f"Event loop was stalled: only {ticks}/25 heartbeat ticks landed. "
            "to_thread offload of busy-wait did not free the event loop.",
        )


class TestASTSourceGuard(unittest.IsolatedAsyncioTestCase):
    """Test 3 (M0 GUARD): AST-parse _play_audio — must use asyncio.to_thread, not plain with."""

    async def test_play_audio_source_uses_to_thread_for_legacy_lock(self):
        """Parse playback_stage.py with AST and assert:
        1. _play_audio contains ZERO plain `with self._legacy_global_audio_lock()` calls.
        2. _play_audio contains at least ONE asyncio.to_thread(self._acquire_legacy_global_lock) call.

        This test FAILS on original code (line 357 uses plain `with`) and PASSES after
        the DAEMON-03 fix. Guards against the M0 anti-pattern: docstring promises the fix
        but code doesn't implement it."""
        tree = ast.parse(PLAYBACK.read_text())

        def find_func(node, name):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
                return node
            for child in ast.iter_child_nodes(node):
                f = find_func(child, name)
                if f is not None:
                    return f
            return None

        play_audio = find_func(tree, "_play_audio")
        self.assertIsNotNone(play_audio, "_play_audio not found in playback_stage.py")

        # Must have ZERO plain `with self._legacy_global_audio_lock()` calls
        plain_with_legacy = [
            n for n in ast.walk(play_audio)
            if isinstance(n, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "_legacy_global_audio_lock"
                for item in n.items
            )
        ]
        self.assertEqual(
            len(plain_with_legacy), 0,
            "_play_audio still uses plain `with self._legacy_global_audio_lock()` — "
            "the asyncio.to_thread offload did not land at the observable callsite "
            "(M0 anti-pattern: docstring promises fix, code does not implement it)",
        )

        # Must have at least ONE asyncio.to_thread(self._acquire_legacy_global_lock)
        to_thread_acquires = [
            n for n in ast.walk(play_audio)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "to_thread"
            and any(
                isinstance(arg, ast.Attribute) and arg.attr == "_acquire_legacy_global_lock"
                for arg in n.args
            )
        ]
        self.assertGreaterEqual(
            len(to_thread_acquires), 1,
            "_play_audio does not call asyncio.to_thread(self._acquire_legacy_global_lock); "
            "the offload did not land at the observable callsite (M0 anti-pattern)",
        )


class TestAcquireRunsOffMainThread(unittest.IsolatedAsyncioTestCase):
    """Test 4 (M0 GUARD): _play_audio must invoke _acquire_legacy_global_lock off the main thread."""

    async def test_play_audio_runs_acquire_off_main_thread(self):
        """Mock _acquire_legacy_global_lock and capture thread ID.
        After await stage._play_audio(...), assert the acquire ran on a WORKER thread,
        not the main asyncio loop thread.

        This test FAILS on original code (no to_thread call; _acquire doesn't exist) and
        PASSES after the DAEMON-03 fix."""
        stage = PlaybackStage()
        captured = {}

        def fake_acquire():
            captured["thread_id"] = threading.current_thread().ident
            captured["thread_name"] = threading.current_thread().name
            return None

        def fake_release(lock_file):
            pass

        async def fake_inner(audio_path, state):
            return True

        stage._acquire_legacy_global_lock = fake_acquire
        stage._release_legacy_global_lock = fake_release
        stage._play_audio_inner = fake_inner

        main_thread_id = threading.current_thread().ident
        state = PlaybackState()
        await stage._play_audio("/dev/null", state)

        self.assertIsNotNone(
            captured.get("thread_id"),
            "_acquire_legacy_global_lock was never called during _play_audio",
        )
        self.assertNotEqual(
            captured["thread_id"],
            main_thread_id,
            f"_acquire_legacy_global_lock ran on the main asyncio loop thread "
            f"({captured.get('thread_name', '?')}); asyncio.to_thread offload did not happen",
        )


class TestReleaseRunsOffMainThread(unittest.IsolatedAsyncioTestCase):
    """Test 5 (M0 GUARD): _play_audio must invoke _release_legacy_global_lock off the main thread."""

    async def test_release_runs_off_main_thread(self):
        """Symmetric to Test 4: mock _release_legacy_global_lock and assert it ran on a worker thread.

        This test FAILS on original code (no to_thread call; _release doesn't exist) and
        PASSES after the DAEMON-03 fix."""
        stage = PlaybackStage()
        captured = {}

        def fake_acquire():
            return None

        def fake_release(lock_file):
            captured["thread_id"] = threading.current_thread().ident
            captured["thread_name"] = threading.current_thread().name

        async def fake_inner(audio_path, state):
            return True

        stage._acquire_legacy_global_lock = fake_acquire
        stage._release_legacy_global_lock = fake_release
        stage._play_audio_inner = fake_inner

        main_thread_id = threading.current_thread().ident
        state = PlaybackState()
        await stage._play_audio("/dev/null", state)

        self.assertIsNotNone(
            captured.get("thread_id"),
            "_release_legacy_global_lock was never called during _play_audio",
        )
        self.assertNotEqual(
            captured["thread_id"],
            main_thread_id,
            f"_release_legacy_global_lock ran on the main asyncio loop thread "
            f"({captured.get('thread_name', '?')}); asyncio.to_thread offload did not happen",
        )


if __name__ == "__main__":
    unittest.main()
