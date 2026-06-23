"""
Tests for Wave 1.E PlaybackStage additions:

- PlaybackState.current_proc field
- AudioSegment.category field (additive on generate_stage.AudioSegment)
- PlaybackStage.priority_enqueue
- PlaybackStage.flush_buffer
- PlaybackStage.get_current_proc

Does NOT exercise pre-existing playback functionality (start_session, the
playback_loop happy path, etc.) — those are covered elsewhere. We only test
the new surface so this file remains green even if the wider pipeline grows
new failure modes.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.pipeline.playback_stage import PlaybackStage, PlaybackState
from daemon.pipeline.generate_stage import AudioSegment
from daemon.tts_types import Category


def _segment(
    session_id: str = "test-session",
    chunk_index: int = 0,
    total_chunks: int = 1,
    category=None,
    request_id: str = "req-1",
    audio_path: str = "/tmp/nonexistent.mp3",
) -> AudioSegment:
    return AudioSegment(
        audio_path=audio_path,
        duration_ms=100.0,
        text=f"chunk-{chunk_index}",
        session_id=session_id,
        request_id=request_id,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        generated_at=0.0,
        category=category,
    )


def test_playback_state_has_current_proc_field():
    state = PlaybackState()
    assert hasattr(state, "current_proc")
    assert state.current_proc is None


def test_audio_segment_has_category_field():
    seg = _segment()
    assert hasattr(seg, "category")
    assert seg.category is None

    err_seg = _segment(category=Category.ERROR)
    assert err_seg.category == Category.ERROR


@pytest.mark.asyncio
async def test_priority_enqueue_lands_at_front_of_buffer():
    stage = PlaybackStage(buffer_size=8)
    sid = "priority-test"

    await stage.start_session(sid)
    try:
        # Pre-load buffer with some normal segments.
        normal_a = _segment(session_id=sid, chunk_index=10, request_id="normal-a")
        normal_b = _segment(session_id=sid, chunk_index=11, request_id="normal-b")
        await stage.enqueue_segment(normal_a)
        await stage.enqueue_segment(normal_b)

        # Inject a high-priority segment.
        priority_seg = _segment(
            session_id=sid,
            chunk_index=99,
            request_id="error-1",
            category=Category.ERROR,
        )
        await stage.priority_enqueue(priority_seg)

        # Drain via the underlying queue and assert order.
        buffer = stage.session_buffers[sid]
        first = buffer.get_nowait()
        second = buffer.get_nowait()
        third = buffer.get_nowait()

        assert first is priority_seg, "priority segment must be at the front"
        assert second is normal_a, "original ordering of pending must be preserved"
        assert third is normal_b
    finally:
        await stage.stop_session(sid)


@pytest.mark.asyncio
async def test_priority_enqueue_lazily_starts_session():
    stage = PlaybackStage(buffer_size=4)
    sid = "lazy-start"

    seg = _segment(session_id=sid, category=Category.ERROR)
    await stage.priority_enqueue(seg)
    try:
        assert sid in stage.session_buffers
        # The segment should be sitting in the freshly-created buffer.
        buf = stage.session_buffers[sid]
        assert buf.get_nowait() is seg
    finally:
        await stage.stop_session(sid)


@pytest.mark.asyncio
async def test_flush_buffer_drops_non_errors_keeps_errors():
    stage = PlaybackStage(buffer_size=8)
    sid = "flush-test"
    await stage.start_session(sid)
    try:
        status_a = _segment(
            session_id=sid, chunk_index=1, request_id="status-a", category=Category.STATUS
        )
        error_a = _segment(
            session_id=sid, chunk_index=2, request_id="error-a", category=Category.ERROR
        )
        insight_a = _segment(
            session_id=sid, chunk_index=3, request_id="insight-a", category=Category.INSIGHT
        )
        error_b = _segment(
            session_id=sid, chunk_index=4, request_id="error-b", category=Category.ERROR
        )
        untagged = _segment(session_id=sid, chunk_index=5, request_id="untagged")

        for s in (status_a, error_a, insight_a, error_b, untagged):
            await stage.enqueue_segment(s)

        dropped = await stage.flush_buffer(sid, keep_errors=True)
        assert dropped == 3, f"expected 3 dropped (status/insight/untagged), got {dropped}"

        # Verify the survivors are exactly the ERROR segments, in original order.
        buf = stage.session_buffers[sid]
        survivors = []
        while True:
            try:
                survivors.append(buf.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert survivors == [error_a, error_b]
    finally:
        await stage.stop_session(sid)


@pytest.mark.asyncio
async def test_flush_buffer_keep_errors_false_drops_everything():
    stage = PlaybackStage(buffer_size=4)
    sid = "flush-everything"
    await stage.start_session(sid)
    try:
        await stage.enqueue_segment(_segment(session_id=sid, chunk_index=1, request_id="a", category=Category.ERROR))
        await stage.enqueue_segment(_segment(session_id=sid, chunk_index=2, request_id="b", category=Category.STATUS))

        dropped = await stage.flush_buffer(sid, keep_errors=False)
        assert dropped == 2
        assert stage.session_buffers[sid].empty()
    finally:
        await stage.stop_session(sid)


@pytest.mark.asyncio
async def test_flush_buffer_unknown_session_is_noop():
    stage = PlaybackStage()
    dropped = await stage.flush_buffer("does-not-exist")
    assert dropped == 0


def test_get_current_proc_unknown_session_returns_none():
    stage = PlaybackStage()
    assert stage.get_current_proc("nope") is None


@pytest.mark.asyncio
async def test_get_current_proc_returns_state_value():
    stage = PlaybackStage()
    sid = "proc-accessor"
    await stage.start_session(sid)
    try:
        # Initially nothing is playing.
        assert stage.get_current_proc(sid) is None

        # Simulate _play_audio populating the field.
        sentinel = object()
        stage.session_states[sid].current_proc = sentinel  # type: ignore[assignment]
        assert stage.get_current_proc(sid) is sentinel

        stage.session_states[sid].current_proc = None
        assert stage.get_current_proc(sid) is None
    finally:
        await stage.stop_session(sid)


@pytest.mark.asyncio
async def test_play_audio_tracks_proc_on_state_via_subprocess():
    """End-to-end: real `sleep 0` subprocess so we can observe current_proc
    being set during the await and cleared afterwards."""
    stage = PlaybackStage()
    state = PlaybackState()

    # Monkeypatch: replace the platform-specific player call with a tiny
    # subprocess that exits successfully. We can't easily intercept mid-await,
    # but we can verify the post-condition (current_proc is None after wait).
    # Use /usr/bin/true equivalent — `sleep 0` is portable on macOS+Linux.
    original_create = asyncio.create_subprocess_exec

    async def fake_create(*args, **kwargs):
        return await original_create("sleep", "0", **{
            k: v for k, v in kwargs.items() if k in ("stdout", "stderr")
        })

    asyncio.create_subprocess_exec = fake_create  # type: ignore[assignment]
    try:
        # _play_audio bails early if the path doesn't exist; create a temp file.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = f.name
        try:
            ok = await stage._play_audio(tmp, state=state)
            assert ok is True
            assert state.current_proc is None, "proc must be cleared in finally"
        finally:
            os.unlink(tmp)
    finally:
        asyncio.create_subprocess_exec = original_create  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# W3.B Part 2 — afplay watchdog tests
#
# Watchdog must SIGTERM `state.current_proc` when no progress is made for
# `_watchdog_stuck_seconds` while there is genuine work pending. It MUST NOT
# fire when the session is idle (empty buffer + no current_proc).
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for an asyncio.subprocess.Process whose returncode is None
    (still running) and whose .terminate() flips it to mimic a kill. The
    watchdog should call .terminate() exactly once."""
    def __init__(self):
        self.returncode = None
        self.terminate_calls = 0
        self.pid = 99999

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15  # SIGTERM


def test_watchdog_started_and_cancelled_with_session():
    """start_session spawns a watchdog task; stop_session cancels it."""
    async def _run():
        stage = PlaybackStage()
        await stage.start_session("w-1")
        assert "w-1" in stage._watchdog_tasks
        wd = stage._watchdog_tasks["w-1"]
        assert not wd.done()
        await stage.stop_session("w-1")
        assert "w-1" not in stage._watchdog_tasks

    asyncio.run(_run())


def test_watchdog_terminates_stuck_proc():
    """With buffer pending + a proc that never finishes, watchdog SIGTERMs it
    after _watchdog_stuck_seconds elapses."""
    async def _run():
        stage = PlaybackStage()
        # Compress timing for the test: stuck after 0.3s, poll every 0.05s.
        stage._watchdog_stuck_seconds = 0.3
        stage._watchdog_poll_seconds = 0.05

        # Hand-build session state without the playback loop (we don't want
        # the loop to actually consume from buffer — we want it to LOOK stuck).
        sid = "w-stuck"
        stage.session_buffers[sid] = asyncio.Queue(maxsize=3)
        stage.session_locks[sid] = asyncio.Lock()
        stage.session_states[sid] = PlaybackState()
        stage._stop_events[sid] = asyncio.Event()

        # Inject pending work + a "running" fake proc.
        stage.session_buffers[sid].put_nowait(_segment(session_id=sid))
        fake_proc = _FakeProc()
        stage.session_states[sid].current_proc = fake_proc  # type: ignore[assignment]

        # Spawn ONLY the watchdog (not the playback loop — we're faking stuck).
        wd = asyncio.create_task(stage._playback_watchdog_loop(sid))

        # Give it long enough to: (1) initialize baseline, (2) detect stall,
        # (3) reach the stuck threshold, (4) call terminate. Total budget
        # ~= poll + stuck + poll for safety = 0.05 + 0.3 + 0.1 = 0.45s.
        # Allow generous slack to avoid CI flake.
        await asyncio.sleep(0.7)

        assert fake_proc.terminate_calls >= 1, (
            f"watchdog must SIGTERM stuck proc; calls={fake_proc.terminate_calls}"
        )

        # Cleanup.
        stage._stop_events[sid].set()
        wd.cancel()
        try:
            await wd
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_watchdog_idle_session_no_action():
    """Empty buffer + no current_proc → watchdog must NOT terminate anything."""
    async def _run():
        stage = PlaybackStage()
        stage._watchdog_stuck_seconds = 0.2
        stage._watchdog_poll_seconds = 0.05

        sid = "w-idle"
        stage.session_buffers[sid] = asyncio.Queue(maxsize=3)
        stage.session_locks[sid] = asyncio.Lock()
        stage.session_states[sid] = PlaybackState()
        stage._stop_events[sid] = asyncio.Event()
        # No buffer entry, no current_proc.

        wd = asyncio.create_task(stage._playback_watchdog_loop(sid))
        await asyncio.sleep(0.5)  # well past stuck threshold

        # Nothing to assert about — there's no proc, so no terminate to count.
        # The behavior we DO assert is that the watchdog hasn't crashed.
        assert not wd.done() or wd.cancelled(), (
            "watchdog should still be running on an idle session"
        )

        stage._stop_events[sid].set()
        wd.cancel()
        try:
            await wd
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_watchdog_progress_resets_timer():
    """When segments_played advances, the stuck timer resets and no SIGTERM
    fires even if a proc has been registered for longer than the threshold."""
    async def _run():
        stage = PlaybackStage()
        stage._watchdog_stuck_seconds = 0.3
        stage._watchdog_poll_seconds = 0.05

        sid = "w-progress"
        stage.session_buffers[sid] = asyncio.Queue(maxsize=3)
        stage.session_locks[sid] = asyncio.Lock()
        stage.session_states[sid] = PlaybackState()
        stage._stop_events[sid] = asyncio.Event()

        stage.session_buffers[sid].put_nowait(_segment(session_id=sid))
        fake_proc = _FakeProc()
        stage.session_states[sid].current_proc = fake_proc  # type: ignore[assignment]

        wd = asyncio.create_task(stage._playback_watchdog_loop(sid))

        # Advance segments_played BEFORE the stuck threshold to reset timer.
        await asyncio.sleep(0.15)
        stage.session_states[sid].segments_played += 1

        # Now wait less than the threshold from the last progress tick.
        await asyncio.sleep(0.2)

        assert fake_proc.terminate_calls == 0, (
            "watchdog must not fire when progress was recently observed"
        )

        stage._stop_events[sid].set()
        wd.cancel()
        try:
            await wd
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
