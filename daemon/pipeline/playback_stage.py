"""
Playback Stage - Per-session audio playback with cross-session serialization.

Audio serialization model (LEGACY-08 decision: single-user-per-daemon):
  - Per-session asyncio.Lock — ensures only one segment plays per session
  - _cross_session_audio_lock (asyncio.Lock) — ensures only one session
    plays at a time across ALL concurrent sessions in this daemon instance
  - fcntl tier removed (Phase 3 LEGACY-08): legacy speak path deleted;
    the singleton PID check at daemon startup enforces one-daemon-per-user
    invariant, so in-process asyncio.Lock is sufficient
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
import logging
import platform
import time

from .generate_stage import AudioSegment

if TYPE_CHECKING:
    from ..tts_types import Category

logger = logging.getLogger(__name__)


@dataclass
class PlaybackState:
    """Track playback state for a session."""
    is_playing: bool = False
    current_segment: Optional[AudioSegment] = None
    segments_played: int = 0
    total_duration_ms: float = 0.0
    errors: int = 0
    # In-flight afplay (or platform equivalent) subprocess; populated for the
    # duration of `await proc.wait()` so QueueManager can SIGTERM it for ERROR
    # pre-emption. None when no segment is actively playing.
    current_proc: Optional[asyncio.subprocess.Process] = None


class PlaybackStage:
    """
    Per-session audio playback with pre-buffering.

    Key improvements:
    - Per-session locks (NOT global) - sessions play independently
    - Pre-buffering for gapless playback
    - Configurable crossfade between segments
    - Platform-aware audio playback (macOS/Linux)
    """

    def __init__(
        self,
        buffer_size: int = 3,
        crossfade_ms: int = 50,
        volume: float = 1.0
    ):
        self.buffer_size = buffer_size
        self.crossfade_ms = crossfade_ms
        # Playback gain passed to `afplay -v`. 1.0 = unmodified; >1.0 amplifies
        # (afplay applies software gain). Wired from config voice.volume — this
        # was previously a no-op (afplay was spawned with no -v flag).
        self.volume = volume

        # Per-session state.
        self.session_states: dict[str, PlaybackState] = {}
        self.session_buffers: dict[str, asyncio.Queue[AudioSegment]] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}

        self._playback_tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, asyncio.Event] = {}

        # W3.B Part 2: per-session afplay watchdog. If the playback loop hangs
        # (audio device disappears mid-playback, driver glitch, kernel
        # weirdness) `proc.wait()` will block forever and the session never
        # advances. The watchdog SIGTERMs `state.current_proc` if
        # `segments_played` hasn't moved in `_watchdog_stuck_seconds` while
        # there is genuine work pending (buffer non-empty OR a proc is
        # supposedly playing). Override threshold for tests.
        self._watchdog_tasks: dict[str, asyncio.Task] = {}
        self._watchdog_stuck_seconds: float = 60.0
        self._watchdog_poll_seconds: float = 5.0

        # Cross-session audio serialization (LEGACY-08: fcntl tier deleted).
        # asyncio.Lock prevents two per-session tasks (in this same daemon)
        # from spawning concurrent afplay subprocesses.  The singleton PID
        # check at daemon startup ensures only one daemon runs per user, so
        # this in-process lock is sufficient for cross-session ordering.
        self._cross_session_audio_lock = asyncio.Lock()

        # Detect platform for audio player selection
        self._platform = platform.system().lower()

        # OBSERVE-02: epoch timestamp of the last successful afplay exit.
        # ``None`` until the first audible segment completes. Used by the
        # `health` socket command + ensure-daemon-ready.sh to detect
        # silent-session-death (audio stops while hooks keep firing).
        self.last_audio_played_at: Optional[float] = None

        # Stats
        self._stats = {
            'segments_played': 0,
            'playback_errors': 0,
            'total_playback_time_ms': 0.0,
        }

    async def start_session(self, session_id: str):
        """Initialize playback for a session."""
        if session_id in self.session_buffers:
            logger.debug(f"Session {session_id} already started")
            return

        self.session_buffers[session_id] = asyncio.Queue(maxsize=self.buffer_size)
        self.session_locks[session_id] = asyncio.Lock()
        self.session_states[session_id] = PlaybackState()
        self._stop_events[session_id] = asyncio.Event()

        def _log_task_exc(t: asyncio.Task, name: str) -> None:
            """DAEMON-04: log exceptions from background tasks (N6 mitigation)."""
            if not t.cancelled() and t.done():
                exc = t.exception()
                if exc is not None:
                    logger.error("background task %s failed: %s", name, exc)

        # Start playback consumer task
        task = asyncio.create_task(
            self._playback_loop(session_id),
            name=f"playback-loop[{session_id}]",
        )
        task.add_done_callback(
            lambda t: _log_task_exc(t, f"playback-loop[{session_id}]")
        )
        self._playback_tasks[session_id] = task

        # W3.B Part 2: spawn watchdog alongside the playback loop.
        watchdog = asyncio.create_task(
            self._playback_watchdog_loop(session_id),
            name=f"playback-watchdog[{session_id}]",
        )
        watchdog.add_done_callback(
            lambda t: _log_task_exc(t, f"playback-watchdog[{session_id}]")
        )
        self._watchdog_tasks[session_id] = watchdog

        logger.debug(f"Started playback session {session_id}")

    async def enqueue_segment(self, segment: AudioSegment):
        """Add segment to session's playback buffer."""
        session_id = segment.session_id

        if session_id not in self.session_buffers:
            await self.start_session(session_id)

        # Block if buffer full (backpressure to generation stage)
        await self.session_buffers[session_id].put(segment)
        logger.debug(
            f"Enqueued segment {segment.chunk_index}/{segment.total_chunks} "
            f"for session {session_id}"
        )

    async def _playback_loop(self, session_id: str):
        """
        Continuous playback loop for a session.

        Consumes from buffer, plays audio, handles gaps.
        Uses per-session lock (NOT global).
        """
        buffer = self.session_buffers[session_id]
        lock = self.session_locks[session_id]
        state = self.session_states[session_id]
        stop_event = self._stop_events[session_id]

        while not stop_event.is_set():
            try:
                # Get next segment with timeout (allows checking stop_event)
                try:
                    segment = await asyncio.wait_for(
                        buffer.get(),
                        timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                async with lock:  # Per-session lock only!
                    state.is_playing = True
                    state.current_segment = segment

                    # Play audio (state passed so _play_audio can publish proc
                    # for ERROR pre-emption via PlaybackStage.get_current_proc).
                    # OBSERVE-01: forward request_id so log lines in
                    # _play_audio_inner share the same UUID as the dispatch
                    # boundary (grep `request_id=<uuid>` end-to-end).
                    success = await self._play_audio(
                        segment.audio_path, state=state,
                        request_id=segment.request_id,
                    )

                    if success:
                        state.segments_played += 1
                        state.total_duration_ms += segment.duration_ms
                        self._stats['segments_played'] += 1
                        self._stats['total_playback_time_ms'] += segment.duration_ms
                    else:
                        state.errors += 1
                        self._stats['playback_errors'] += 1

                    state.is_playing = False
                    state.current_segment = None

                # Minimal gap between segments
                if self.crossfade_ms > 0:
                    await asyncio.sleep(self.crossfade_ms / 1000)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Playback error in session {session_id}: {e}")
                state.errors += 1
                self._stats['playback_errors'] += 1
                continue

        logger.debug(f"Playback loop ended for session {session_id}")

    async def _playback_watchdog_loop(self, session_id: str):
        """Watchdog: SIGTERM a hung afplay subprocess.

        afplay normally returns when the audio file finishes. If the audio
        device disappears (USB unplug, sleep/wake transition, driver glitch),
        `proc.wait()` in `_play_audio_inner` can block indefinitely and the
        playback loop never advances — the buffer stops draining and TTS
        appears dead even though hooks are still firing.

        This watchdog polls every `_watchdog_poll_seconds`. If
        `state.segments_played` hasn't moved in `_watchdog_stuck_seconds`
        AND there is genuine work pending (buffer non-empty OR a `current_proc`
        is registered), we SIGTERM the proc. Returning from `proc.wait()`
        unblocks the loop, the segment is counted as failed, and the loop
        moves on to the next one.

        Idle sessions (empty buffer, no current_proc) reset the timer — we
        only care about *stuck* sessions, not quiet ones.
        """
        stop_event = self._stop_events.get(session_id)
        if stop_event is None:
            return

        last_seen_count = -1
        last_progress_at = time.time()

        while not stop_event.is_set():
            try:
                await asyncio.sleep(self._watchdog_poll_seconds)
            except asyncio.CancelledError:
                break

            state = self.session_states.get(session_id)
            buffer = self.session_buffers.get(session_id)
            if state is None or buffer is None:
                return  # session torn down beneath us

            # Initialize baseline on first tick.
            if last_seen_count == -1:
                last_seen_count = state.segments_played
                last_progress_at = time.time()
                continue

            # Progress observed → reset timer.
            if state.segments_played != last_seen_count:
                last_seen_count = state.segments_played
                last_progress_at = time.time()
                continue

            # No progress. Is the session genuinely idle?
            if buffer.empty() and state.current_proc is None:
                last_progress_at = time.time()
                continue

            # No progress AND there is work pending. Has the freeze gone on
            # long enough to act?
            stuck_for = time.time() - last_progress_at
            if stuck_for < self._watchdog_stuck_seconds:
                continue

            proc = state.current_proc
            if proc is not None and proc.returncode is None:
                logger.warning(
                    "Playback watchdog: session %s stuck for %.1fs; "
                    "sending SIGTERM to afplay (pid=%s)",
                    session_id, stuck_for, getattr(proc, "pid", "?"),
                )
                try:
                    proc.terminate()
                except (ProcessLookupError, AttributeError):
                    pass
            else:
                # Buffer non-empty but no proc — generation/playback handoff
                # may be wedged. Nothing the watchdog can SIGTERM here; just
                # log and roll the timer forward to avoid spamming.
                logger.warning(
                    "Playback watchdog: session %s no progress for %.1fs and "
                    "no current_proc to terminate (buffer=%d)",
                    session_id, stuck_for, buffer.qsize(),
                )

            # Reset timer so we don't spam SIGTERMs every poll cycle.
            last_progress_at = time.time()

        logger.debug(f"Watchdog loop ended for session {session_id}")

    # LEGACY-08: _legacy_global_audio_lock, _acquire_legacy_global_lock,
    # _release_legacy_global_lock — all deleted (Phase 3).
    # fcntl coexistence tier was only needed while tts_daemon.speak_text held
    # the same lock file. speak_text is now deleted; only this pipeline writes
    # audio. The in-process _cross_session_audio_lock is sufficient.

    async def _play_audio(
        self,
        audio_path: str,
        state: Optional[PlaybackState] = None,
        request_id: Optional[str] = None,
    ) -> bool:
        """
        Play audio file asynchronously.

        Platform-aware: uses afplay on macOS, mpv/aplay on Linux.
        Uses create_subprocess_exec (safe, no shell injection).

        If `state` is provided, the in-flight subprocess is tracked on
        `state.current_proc` so QueueManager can pre-empt it (SIGTERM)
        when an ERROR-tier item arrives.

        OBSERVE-01: ``request_id`` is logged at start AND completion so a
        single grep over tts_daemon.log returns ≥2 hits at the playback
        boundary. ``None`` is acceptable (back-compat for direct callers).

        AUDIO-01 / LEGACY-08: serializes via _cross_session_audio_lock only
        (asyncio.Lock, in-process).  fcntl cross-process tier deleted — the
        daemon's own singleton PID check ensures one daemon per user.
        """
        if not os.path.exists(audio_path):
            logger.warning(f"Audio file not found: {audio_path}")
            return False

        async with self._cross_session_audio_lock:
            return await self._play_audio_inner(audio_path, state, request_id)

    async def _play_audio_inner(
        self,
        audio_path: str,
        state: Optional[PlaybackState] = None,
        request_id: Optional[str] = None,
    ) -> bool:
        """Inner playback — caller has already acquired _cross_session_audio_lock.

        AUDIO-03: the try/finally below ensures the subprocess transport is
        closed on CancelledError so file descriptors do not leak even when the
        task is cancelled mid-playback (CPython issue #100055).
        """
        proc = None
        # OBSERVE-01: log at playback start so request_id is traceable
        # at the boundary where audio actually begins.
        logger.info(
            f"playback start request_id={request_id} path={audio_path}"
        )
        try:
            if self._platform == 'darwin':
                # macOS - using create_subprocess_exec (safe, no shell injection)
                # AUDIO-02: canonical afplay spawn site — this is the only one.
                afplay_cmd = ['afplay']
                if self.volume and self.volume != 1.0:
                    afplay_cmd += ['-v', f'{self.volume:.3f}']
                afplay_cmd.append(audio_path)
                proc = await asyncio.create_subprocess_exec(
                    *afplay_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            elif self._platform == 'linux':
                # Linux - try mpv first
                proc = await asyncio.create_subprocess_exec(
                    'mpv', '--no-video', '--really-quiet', audio_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            else:
                # Windows or other - try ffplay
                proc = await asyncio.create_subprocess_exec(
                    'ffplay', '-nodisp', '-autoexit', audio_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )

            if state is not None:
                state.current_proc = proc
            try:
                await proc.wait()
            finally:
                if state is not None:
                    state.current_proc = None
                # AUDIO-03: explicitly close transport on cancellation so that
                # file descriptors are not held open (CPython issue #100055).
                # DEVNULL subprocesses have no stdout/stderr streams to close,
                # but closing the transport releases the OS-level process handle.
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except (ProcessLookupError, AttributeError):
                        pass
                    # Reap the process to avoid zombie; best-effort.
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass
            success = proc.returncode == 0
            # OBSERVE-02: record successful-playback timestamp at the
            # exact instant afplay exits with rc=0.  Used by the `health`
            # socket command + ensure-daemon-ready.sh staleness check.
            if success:
                self.last_audio_played_at = time.time()
            # OBSERVE-01: log at completion so request_id is traceable
            # at both ends of the playback boundary.
            logger.info(
                f"playback end request_id={request_id} rc={proc.returncode} "
                f"ok={success}"
            )
            return success

        except FileNotFoundError as e:
            logger.error(f"Audio player not found: {e}")
            return False
        except asyncio.CancelledError:
            # Propagate CancelledError but ensure proc is terminated first.
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, AttributeError):
                    pass
            raise
        except Exception as e:
            logger.error(f"Failed to play audio: {e}")
            return False

    async def stop_session(self, session_id: str):
        """Stop playback and cleanup for a session."""
        # Signal stop
        if session_id in self._stop_events:
            self._stop_events[session_id].set()

        # Cancel playback task
        if session_id in self._playback_tasks:
            self._playback_tasks[session_id].cancel()
            try:
                await self._playback_tasks[session_id]
            except asyncio.CancelledError:
                pass
            del self._playback_tasks[session_id]

        # W3.B Part 2: cancel watchdog task
        if session_id in self._watchdog_tasks:
            self._watchdog_tasks[session_id].cancel()
            try:
                await self._watchdog_tasks[session_id]
            except asyncio.CancelledError:
                pass
            del self._watchdog_tasks[session_id]

        # Cleanup resources
        if session_id in self.session_buffers:
            del self.session_buffers[session_id]
        if session_id in self.session_locks:
            del self.session_locks[session_id]
        if session_id in self.session_states:
            del self.session_states[session_id]
        if session_id in self._stop_events:
            del self._stop_events[session_id]

        logger.debug(f"Stopped playback session {session_id}")

    def get_session_state(self, session_id: str) -> Optional[PlaybackState]:
        """Get current playback state for a session."""
        return self.session_states.get(session_id)

    def get_stats(self) -> dict:
        """Get playback statistics."""
        return {
            **self._stats,
            'active_sessions': len(self.session_buffers),
            'sessions_playing': sum(
                1 for state in self.session_states.values()
                if state.is_playing
            ),
        }

    async def pause_session(self, session_id: str):
        """Pause playback for a session (future enhancement)."""
        pass

    async def resume_session(self, session_id: str):
        """Resume playback for a session (future enhancement)."""
        pass

    # ===== QueueManager-facing API (Wave 1.E additions) =====

    async def priority_enqueue(self, segment: AudioSegment) -> None:
        """Insert segment at the FRONT of the per-session playback buffer.

        Used by QueueManager.submit_priority for ERROR pre-emption — the next
        segment popped from the buffer will be this one, not whatever was
        already queued.

        Implementation: asyncio.Queue lacks insert(), so we drain to a list,
        prepend the new segment, then refill in original order. Acquires the
        per-session lock to keep the operation atomic against the playback
        consumer. Adds <=2ms overhead for default 3-segment buffers.

        Does NOT terminate a currently-playing afplay subprocess; the caller
        must do that separately via get_current_proc(session_id).terminate().
        """
        session_id = segment.session_id

        # Lazily start the session so the first ERROR for a session works.
        if session_id not in self.session_buffers:
            await self.start_session(session_id)

        buffer = self.session_buffers[session_id]
        lock = self.session_locks[session_id]

        async with lock:
            # Drain everything currently buffered (non-blocking).
            existing: list[AudioSegment] = []
            while True:
                try:
                    existing.append(buffer.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Prepend the priority segment, then refill in original order.
            # put_nowait can raise QueueFull if buffer_size was tight, but the
            # priority segment itself MUST land — so put it first, then pad
            # the rest until we hit capacity. Anything that doesn't fit is
            # logged and dropped (the queue is supposed to be small anyway).
            try:
                buffer.put_nowait(segment)
            except asyncio.QueueFull:
                # Buffer is at capacity even after drain (shouldn't happen
                # since we just emptied it, but defend the contract).
                logger.warning(
                    f"priority_enqueue: buffer full for session {session_id}; "
                    f"could not insert priority segment"
                )
                # Restore the drained items before bailing out.
                for s in existing:
                    try:
                        buffer.put_nowait(s)
                    except asyncio.QueueFull:
                        break
                return

            for s in existing:
                try:
                    buffer.put_nowait(s)
                except asyncio.QueueFull:
                    logger.debug(
                        f"priority_enqueue: dropping displaced segment "
                        f"{s.chunk_index}/{s.total_chunks} for session "
                        f"{session_id} (buffer capacity reached after refill)"
                    )
                    break

        logger.debug(
            f"Priority-enqueued segment for session {session_id} "
            f"(displaced {len(existing)} pending segments)"
        )

    async def flush_buffer(
        self,
        session_id: str,
        keep_errors: bool = True,
    ) -> int:
        """Drain the playback buffer for a session.

        Used by QueueManager when escalating to RED/BLACK tier — the in-flight
        playback continues uninterrupted, but everything pending is dropped.

        Args:
            session_id: Which session's buffer to flush.
            keep_errors: If True (default), segments tagged with
                category == Category.ERROR are preserved (re-enqueued in their
                original order after the flush). ERROR audio must never be
                silently dropped.

        Returns:
            Count of segments actually dropped (excludes preserved ERRORs).

        Notes:
            - Does NOT kill the currently-playing subprocess. Callers wanting
              that should additionally call get_current_proc().terminate().
            - No-op (returns 0) if the session has no buffer yet.
        """
        if session_id not in self.session_buffers:
            return 0

        # Local import keeps daemon.tts_types out of the module-load critical path
        # and avoids a circular-import risk if tts_types.py ever grows imports.
        from ..tts_types import Category as _Category

        buffer = self.session_buffers[session_id]
        lock = self.session_locks[session_id]
        dropped = 0

        async with lock:
            preserved: list[AudioSegment] = []

            while True:
                try:
                    seg = buffer.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if keep_errors and seg.category == _Category.ERROR:
                    preserved.append(seg)
                else:
                    dropped += 1

            # Re-enqueue ERROR segments in their original order so they keep
            # their relative positions among each other (just minus the
            # non-error items that used to sit between them).
            for seg in preserved:
                try:
                    buffer.put_nowait(seg)
                except asyncio.QueueFull:
                    logger.warning(
                        f"flush_buffer: could not re-enqueue preserved ERROR "
                        f"segment for session {session_id} (buffer full)"
                    )
                    # Count it as dropped since it didn't make it back in.
                    dropped += 1

        logger.debug(
            f"Flushed playback buffer for session {session_id}: "
            f"dropped {dropped}, preserved {len(preserved)} ERROR segment(s)"
        )
        return dropped

    def get_current_proc(
        self,
        session_id: str,
    ) -> Optional[asyncio.subprocess.Process]:
        """Return the in-flight playback subprocess, if any.

        QueueManager calls .terminate() on the result for ERROR mid-segment
        pre-emption (after also calling priority_enqueue to seed the next
        segment).

        Returns None if the session is unknown OR if no segment is actively
        playing right now.
        """
        state = self.session_states.get(session_id)
        if state is None:
            return None
        return state.current_proc
