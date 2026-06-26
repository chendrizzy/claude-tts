"""
Generate Stage - Parallel TTS generation with pre-generation queue.

Generates audio ahead of playback for seamless streaming using
worker pool with semaphore-based concurrency control.
"""

import asyncio
import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from typing import Optional, AsyncIterator, TYPE_CHECKING
import logging

from .process_stage import ProcessedMessage

if TYPE_CHECKING:
    from ..tts_types import Category

logger = logging.getLogger(__name__)

# Disk guard: keep this much headroom on the cache volume. Below it we evict
# aggressively, and if that doesn't help we refuse to synthesize and warn LOUDLY
# rather than letting the write fail silently (the recurring disk-full mute).
MIN_FREE_BYTES_DEFAULT = 200 * 1024 * 1024  # 200 MB

# Engines that emit a WAVE container; their cache files must be named .wav so
# extension-keyed players (macOS afplay) open them. edge-tts emits mp3.
_WAV_ENGINES = ("kokoro", "mlx-audio", "say", "espeak", "system")


def _audio_ext_for(engine: str) -> str:
    """Cache-file extension for an engine's container ('wav' or 'mp3')."""
    return "wav" if str(engine or "").lower() in _WAV_ENGINES else "mp3"


@dataclass
class AudioSegment:
    """Generated audio segment ready for playback."""
    audio_path: str
    duration_ms: float
    text: str
    session_id: str
    request_id: str
    chunk_index: int
    total_chunks: int
    generated_at: float
    # Optional category tag for downstream pre-emption / flush logic.
    # Populated by callers that route via ContentRouter -> RouterDecision.
    # Plain Optional[object] type at runtime to avoid forcing types import here;
    # daemon.tts_types.Category is the canonical source.
    category: Optional["Category"] = None


class GenerateStage:
    """
    Parallel TTS generation with pre-generation queue.

    Key features:
    - Parallel workers per session (configurable, default 3)
    - Audio file caching to avoid regeneration
    - Semaphore-based concurrency control
    - Ordered yield despite parallel generation
    """

    def __init__(
        self,
        workers_per_session: int = 3,
        pre_generate_count: int = 2,
        cache_dir: str = "/tmp/tts_audio_cache",
        voice: str = "en-US-AriaNeural",
        engine: str = "edge-tts",
        speed: float = 1.0,
        mlx_python: Optional[str] = None,
        kokoro_model: Optional[str] = None,
        voicebox_config: Optional[dict] = None,
        min_free_bytes: int = MIN_FREE_BYTES_DEFAULT,
    ):
        self.workers_per_session = workers_per_session
        self.pre_generate_count = pre_generate_count
        self.cache_dir = cache_dir
        self.min_free_bytes = min_free_bytes
        self._last_disk_warn = 0.0  # throttle for the loud low-disk alert
        self.default_voice = voice
        # Engine selection (R-engine): "kokoro"/"mlx-audio" → local MLX Kokoro
        # via a persistent worker subprocess; anything else → edge-tts (Azure).
        self.engine = (engine or "edge-tts").lower()
        self.speed = speed
        self._mlx_python = mlx_python
        self._kokoro_model = kokoro_model
        self._kokoro = None  # lazily constructed KokoroEngine
        # Output container differs per engine: Kokoro/say/espeak write WAV;
        # edge-tts returns MP3. Cache files are named with the correct extension
        # so extension-keyed players (macOS afplay) open them correctly.
        self._audio_ext = _audio_ext_for(self.engine)

        # Voicebox backend (config-gated): when engine=="voicebox", synthesis +
        # playback are offloaded to the local Voicebox app and generate() yields
        # no segments. OFF by default — the client module isn't imported otherwise.
        self._voicebox = None
        if self.engine == "voicebox":
            from .voicebox_client import VoiceboxClient
            vc = voicebox_config or {}
            self._voicebox = VoiceboxClient(
                url=vc.get("url", "http://127.0.0.1:17493"),
                profile_id=vc.get("profile_id"),
                engine=vc.get("engine"),
                personality=vc.get("personality", False),
                cleanup=vc.get("cleanup", True),
                timeout_s=float(vc.get("timeout_s", 10.0)),
            )

        # Per-session resources
        self.audio_queues: dict[str, asyncio.Queue[AudioSegment]] = {}
        self.session_semaphores: dict[str, asyncio.Semaphore] = {}
        self.generation_tasks: dict[str, list[asyncio.Task]] = {}

        # Stats
        self._stats = {
            'segments_generated': 0,
            'cache_hits': 0,
            'generation_errors': 0,
            'total_generation_time_ms': 0.0,
        }

        # Ensure cache directory exists
        os.makedirs(cache_dir, exist_ok=True)

        # Lazy-constructed engine via make_engine (edge-tts/say/espeak).
        self._engine = None  # cached make_engine result (edge-tts/say/espeak)

    def _get_engine(self):
        """Lazily construct the configured stateless engine via make_engine.

        Covers edge-tts / say / espeak. Kokoro keeps its own async getter
        (`_get_kokoro`) — it owns a persistent subprocess worker.
        """
        if self._engine is None:
            from daemon.engines import make_engine
            self._engine = make_engine(self.engine)
        return self._engine

    async def _get_kokoro(self):
        """Lazily construct and start the persistent Kokoro worker engine."""
        if self._kokoro is None:
            from .kokoro_engine import (
                KokoroEngine, DEFAULT_MLX_PYTHON, DEFAULT_MODEL,
            )
            self._kokoro = KokoroEngine(
                mlx_python=self._mlx_python or DEFAULT_MLX_PYTHON,
                model=self._kokoro_model or DEFAULT_MODEL,
                default_voice=self.default_voice,
            )
            ok = await self._kokoro.start()
            if not ok:
                logger.error("Kokoro engine failed to start")
        return self._kokoro

    async def generate(
        self,
        processed: ProcessedMessage,
        voice: Optional[str] = None
    ) -> AsyncIterator[AudioSegment]:
        """
        Generate audio for all chunks in parallel.

        Yields segments in order (may complete out of order internally).
        Uses semaphore to limit concurrent generations per session.
        """
        session_id = processed.session_id
        voice = voice or self.default_voice

        # Initialize session resources
        if session_id not in self.audio_queues:
            self.audio_queues[session_id] = asyncio.Queue()
            self.session_semaphores[session_id] = asyncio.Semaphore(
                self.workers_per_session
            )

        semaphore = self.session_semaphores[session_id]

        # Handle empty chunks
        if not processed.chunks:
            return

        # Voicebox backend: offload synthesis + playback to the local Voicebox
        # app and yield nothing (PlaybackStage no-ops on empty output — an
        # established safe contract). Voicebox does its own chunking/crossfade,
        # so we hand it the joined utterance. Fail-safe: on any error speak()
        # returns None and we simply produce no audio (never crashes the pipe).
        if self._voicebox is not None:
            text = " ".join(c for c in processed.chunks if c).strip()
            await self._voicebox.speak(text)
            return

        # Launch parallel generation tasks
        tasks = []
        for i, chunk in enumerate(processed.chunks):
            task = asyncio.create_task(
                self._generate_chunk(
                    chunk=chunk,
                    session_id=session_id,
                    request_id=processed.request_id,
                    chunk_index=i,
                    total_chunks=len(processed.chunks),
                    voice=voice,
                    semaphore=semaphore
                )
            )
            tasks.append(task)

        # Track tasks for cleanup
        if session_id not in self.generation_tasks:
            self.generation_tasks[session_id] = []
        self.generation_tasks[session_id].extend(tasks)

        # Yield segments in order as they complete
        completed = {}
        next_expected = 0

        for coro in asyncio.as_completed(tasks):
            try:
                segment = await coro
                if segment:
                    completed[segment.chunk_index] = segment

                    # Yield in order
                    while next_expected in completed:
                        yield completed.pop(next_expected)
                        next_expected += 1
            except Exception as e:
                logger.error(f"Generation task failed: {e}")
                self._stats['generation_errors'] += 1

    async def _ensure_disk_space(self, session_id: str) -> bool:
        """Just-in-time disk guard. True if there's room to synthesize a chunk.

        On low space: evict aggressively (5-min age) and re-check; if still low,
        fire a LOUD signal and return False so the chunk is skipped *audibly*
        (via the alert) instead of the daemon silently muting when the write
        later fails on a full volume. Best-effort: any error returns True — a
        guard bug must never be the reason TTS stops working.
        """
        try:
            if shutil.disk_usage(self.cache_dir).free >= self.min_free_bytes:
                return True
            try:
                await self.cleanup_old_cache(max_age_seconds=300)
            except Exception:
                pass
            if shutil.disk_usage(self.cache_dir).free >= self.min_free_bytes:
                return True
            self._signal_disk_full(session_id, shutil.disk_usage(self.cache_dir).free)
            return False
        except Exception:
            return True

    def _signal_disk_full(self, session_id: str, free_bytes: int) -> None:
        """Loud, throttled low-disk alert. The desktop notification needs NO disk
        write (so it fires even at ~0 bytes free); the spoken-log warning is
        best-effort for the statusline (works because the guard fires while
        headroom remains)."""
        now = time.time()
        if now - self._last_disk_warn < 60:        # at most one alert per minute
            return
        self._last_disk_warn = now
        mb = max(0, free_bytes // (1024 * 1024))
        msg = f"claude-tts muted: only {mb} MB free — free disk space to restore speech"
        try:
            import sys
            import subprocess
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{msg}" with title "claude-tts"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "claude-tts", msg],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            from .. import spoken_log
            spoken_log.append("TTS muted - disk full", session_id=session_id,
                              category="disk_full")
        except Exception:
            pass
        logger.error("DISK GUARD: %s", msg)

    async def _generate_chunk(
        self,
        chunk: str,
        session_id: str,
        request_id: str,
        chunk_index: int,
        total_chunks: int,
        voice: str,
        semaphore: asyncio.Semaphore
    ) -> Optional[AudioSegment]:
        """Generate audio for a single chunk with concurrency control."""

        async with semaphore:  # Limit concurrent generations
            start_time = time.time()

            try:
                # Generate unique filename based on content
                chunk_hash = hashlib.md5(
                    f"{voice}_{chunk}".encode()
                ).hexdigest()[:12]
                audio_path = os.path.join(
                    self.cache_dir,
                    f"{session_id}_{request_id}_{chunk_index}_{chunk_hash}.{self._audio_ext}"
                )

                # Check cache
                if os.path.exists(audio_path):
                    self._stats['cache_hits'] += 1
                    duration = await self._get_audio_duration(audio_path)
                    logger.debug(
                        f"Cache hit for chunk {chunk_index}/{total_chunks}"
                    )
                    return AudioSegment(
                        audio_path=audio_path,
                        duration_ms=duration,
                        text=chunk,
                        session_id=session_id,
                        request_id=request_id,
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        generated_at=time.time()
                    )

                # Disk guard: a cache hit above needs no write, but synthesizing
                # does. Refuse (and warn loudly) when the volume is nearly full
                # instead of silently muting when the write fails on a full disk.
                if not await self._ensure_disk_space(session_id):
                    return None

                # Generate audio via the TTSEngine seam (kokoro keeps its own
                # persistent-worker lazy spawn; edge-tts is a thin EdgeTTSEngine).
                if self.engine in ("kokoro", "mlx-audio"):
                    engine = await self._get_kokoro()
                else:
                    engine = self._get_engine()
                ok = await engine.synthesize(chunk, audio_path, voice, self.speed)
                if not ok:
                    logger.error(f"{self.engine} synth failed for chunk {chunk_index}")
                    self._stats['generation_errors'] += 1
                    return None

                generation_time_ms = (time.time() - start_time) * 1000
                self._stats['total_generation_time_ms'] += generation_time_ms
                self._stats['segments_generated'] += 1

                duration = await self._get_audio_duration(audio_path)

                logger.debug(
                    f"Generated chunk {chunk_index}/{total_chunks} in "
                    f"{generation_time_ms:.0f}ms"
                )

                return AudioSegment(
                    audio_path=audio_path,
                    duration_ms=duration,
                    text=chunk,
                    session_id=session_id,
                    request_id=request_id,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    generated_at=time.time()
                )

            except Exception as e:
                logger.error(f"Failed to generate chunk {chunk_index}: {e}")
                self._stats['generation_errors'] += 1
                return None

    async def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration in milliseconds using ffprobe."""
        try:
            # Using create_subprocess_exec (safe, no shell injection)
            proc = await asyncio.create_subprocess_exec(
                'ffprobe', '-i', audio_path,
                '-show_entries', 'format=duration',
                '-v', 'quiet', '-of', 'csv=p=0',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            duration_str = stdout.decode().strip()
            if duration_str:
                return float(duration_str) * 1000
            return 0.0
        except Exception as e:
            logger.debug(f"Could not get audio duration: {e}")
            return 0.0

    async def cleanup_session(self, session_id: str):
        """Clean up session resources and cached audio files."""
        # Cancel pending tasks
        if session_id in self.generation_tasks:
            for task in self.generation_tasks[session_id]:
                if not task.done():
                    task.cancel()
            del self.generation_tasks[session_id]

        # Clean up queues and semaphores
        if session_id in self.audio_queues:
            del self.audio_queues[session_id]
        if session_id in self.session_semaphores:
            del self.session_semaphores[session_id]

        # Clean cached files for session (both engines' containers).
        import glob
        for ext in ("mp3", "wav"):
            pattern = os.path.join(self.cache_dir, f"{session_id}_*.{ext}")
            for f in glob.glob(pattern):
                try:
                    os.unlink(f)
                except Exception as e:
                    logger.debug(f"Could not delete {f}: {e}")

        logger.debug(f"Cleaned up generate resources for session {session_id}")

    def get_stats(self) -> dict:
        """Get generation statistics."""
        avg_time = 0.0
        if self._stats['segments_generated'] > 0:
            avg_time = (
                self._stats['total_generation_time_ms'] /
                self._stats['segments_generated']
            )
        stats = {
            **self._stats,
            'engine': self.engine,
            'avg_generation_time_ms': avg_time,
            'active_sessions': len(self.session_semaphores),
        }
        if self._kokoro is not None:
            stats['kokoro'] = self._kokoro.get_stats()
        return stats

    async def warm(self):
        """Pre-spawn + warm the Kokoro worker so the first real utterance is
        fast. No-op for non-Kokoro engines. Safe to call once at startup; the
        worker self-warms on load (~4s one-time), which would otherwise be paid
        on the user's first utterance instead."""
        if self.engine in ("kokoro", "mlx-audio"):
            try:
                await self._get_kokoro()
            except Exception as e:
                logger.warning(f"Kokoro warm failed (will retry lazily): {e}")

    async def shutdown(self):
        """Tear down the persistent Kokoro worker, if any."""
        if self._kokoro is not None:
            try:
                await self._kokoro.shutdown()
            except Exception as e:
                logger.debug(f"Kokoro shutdown error: {e}")

    async def cleanup_old_cache(self, max_age_seconds: int = 3600):
        """Clean up cached audio files older than max_age."""
        import glob
        cutoff_time = time.time() - max_age_seconds

        cleaned = 0
        for ext in ("mp3", "wav"):
            for f in glob.glob(os.path.join(self.cache_dir, f"*.{ext}")):
                try:
                    if os.path.getmtime(f) < cutoff_time:
                        os.unlink(f)
                        cleaned += 1
                except Exception:
                    pass

        if cleaned > 0:
            logger.info(f"Cleaned {cleaned} old audio cache files")
