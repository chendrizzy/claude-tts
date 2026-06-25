"""Regression gate for the long-utterance playback cutoff bug.

Symptom: long spoken output (e.g. a commit message read at the Stop hook) was
"abruptly cut off before it finishes."

Root cause: a long utterance fills the per-session playback buffer with its own
later chunks. When the *next* event arrives, QueueManager.predicted_lag_ms counts
those buffered chunks as drift, escalates the session to the BLACK tier, and
_handle_black calls PlaybackStage.flush_buffer(keep_errors=True) — which used to
drop every non-ERROR buffered segment, including the tail of the utterance the
user is currently hearing. The drift layer was treating one long intentional
utterance as backlog to discard.

Fix: flush_buffer also preserves the chunks of the in-progress utterance, matched
by a persisted PlaybackState.current_request_id (persisted because the playback
loop nulls current_segment inside the per-session lock that flush_buffer must
acquire — so current_segment is always None at flush time and cannot be used).

These tests are SYNC (driving the async stage via asyncio.run) so they run in the
all-sync `make verify` gate, which does not load pytest-asyncio.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.pipeline.playback_stage import PlaybackStage, PlaybackState
from daemon.pipeline.generate_stage import AudioSegment
from daemon.tts_types import Category


def _segment(session_id, chunk_index, request_id, category=None):
    return AudioSegment(
        audio_path="/tmp/nonexistent.wav",
        duration_ms=4000.0,  # multi-second sentence — what trips the lag tiers
        text=f"chunk-{chunk_index}",
        session_id=session_id,
        request_id=request_id,
        chunk_index=chunk_index,
        total_chunks=9,
        generated_at=0.0,
        category=category,
    )


def test_playback_state_has_current_request_id_field():
    """PlaybackState exposes the persisted in-progress-utterance id (default None)."""
    state = PlaybackState()
    assert hasattr(state, "current_request_id")
    assert state.current_request_id is None


def _drain(buf):
    out = []
    while True:
        try:
            out.append(buf.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def test_flush_buffer_preserves_current_utterance():
    """A keep_errors flush must NOT drop the tail of the utterance being spoken.

    Only the genuinely-distinct newer message (drift) is dropped.
    """
    async def scenario():
        stage = PlaybackStage(buffer_size=8)
        sid = "cutoff-current"
        await stage.start_session(sid)
        try:
            # "utter-1" is mid-playback; its id is persisted on state.
            stage.session_states[sid].current_request_id = "utter-1"

            tail_2 = _segment(sid, 2, "utter-1", Category.STATUS)
            tail_3 = _segment(sid, 3, "utter-1", Category.STATUS)
            drift = _segment(sid, 0, "utter-2", Category.STATUS)
            for s in (tail_2, tail_3, drift):
                await stage.enqueue_segment(s)

            dropped = await stage.flush_buffer(sid, keep_errors=True)
            survivors = _drain(stage.session_buffers[sid])
            return dropped, survivors, (tail_2, tail_3)
        finally:
            await stage.stop_session(sid)

    dropped, survivors, expected_tail = asyncio.run(scenario())
    assert dropped == 1, f"only the distinct drift message should drop, got {dropped}"
    assert survivors == list(expected_tail), "current-utterance tail must survive, in order"


def test_flush_buffer_current_utterance_and_errors_both_survive():
    """Errors AND the in-progress utterance both survive; unrelated status drops."""
    async def scenario():
        stage = PlaybackStage(buffer_size=8)
        sid = "cutoff-mixed"
        await stage.start_session(sid)
        try:
            stage.session_states[sid].current_request_id = "utter-1"
            tail = _segment(sid, 2, "utter-1", Category.STATUS)
            err = _segment(sid, 5, "err-1", Category.ERROR)
            other = _segment(sid, 0, "utter-2", Category.STATUS)
            for s in (tail, err, other):
                await stage.enqueue_segment(s)
            dropped = await stage.flush_buffer(sid, keep_errors=True)
            return dropped, _drain(stage.session_buffers[sid]), (tail, err)
        finally:
            await stage.stop_session(sid)

    dropped, survivors, expected = asyncio.run(scenario())
    assert dropped == 1, f"only the unrelated status should drop, got {dropped}"
    assert survivors == list(expected), "tail + error survive in original order"


def test_flush_buffer_no_current_utterance_unchanged():
    """Back-compat: with no current utterance, keep_errors flush behaves as before
    (drops all non-ERROR), so the existing drift-protection is unchanged."""
    async def scenario():
        stage = PlaybackStage(buffer_size=8)
        sid = "cutoff-none"
        await stage.start_session(sid)
        try:
            # current_request_id stays None (nothing playing).
            a = _segment(sid, 0, "m-a", Category.STATUS)
            b = _segment(sid, 1, "m-b", Category.INSIGHT)
            err = _segment(sid, 2, "m-err", Category.ERROR)
            for s in (a, b, err):
                await stage.enqueue_segment(s)
            dropped = await stage.flush_buffer(sid, keep_errors=True)
            return dropped, _drain(stage.session_buffers[sid]), err
        finally:
            await stage.stop_session(sid)

    dropped, survivors, err = asyncio.run(scenario())
    assert dropped == 2, f"both status/insight drop when nothing is playing, got {dropped}"
    assert survivors == [err], "only the error survives (unchanged legacy behavior)"
