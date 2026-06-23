"""
Pipeline Adapter - Bridge between async pipeline and threaded daemon.

Provides a synchronous interface for the existing TTSDaemon to use the
new async pipeline infrastructure while maintaining backward compatibility.
"""

import asyncio
import threading
import time
from typing import Optional, Dict, Any, Callable, TYPE_CHECKING
from concurrent.futures import ThreadPoolExecutor
import logging

from .orchestrator import TTSPipelineOrchestrator
from .ingest_stage import IngestMessage
from .process_stage import ProcessStage

if TYPE_CHECKING:
    from .queue_manager import QueueManager

logger = logging.getLogger(__name__)


class PipelineAdapter:
    """
    Adapter bridging async pipeline with synchronous daemon code.

    Manages an event loop in a background thread, allowing the
    threaded TTSDaemon to submit work to the async pipeline.
    """

    def __init__(
        self,
        voice: str = "en-US-AriaNeural",
        workers_per_session: int = 3,
        chunk_size: int = 150,
        buffer_size: int = 3,
        cache_dir: str = "/tmp/tts_audio_cache",
        queue_manager: Optional["QueueManager"] = None,
        engine: str = "edge-tts",
        speed: float = 1.0,
        mlx_python: Optional[str] = None,
        kokoro_model: Optional[str] = None,
        voicebox_config: Optional[dict] = None,
        volume: float = 1.0,
    ):
        self._voice = voice
        self._workers_per_session = workers_per_session
        self._chunk_size = chunk_size
        self._buffer_size = buffer_size
        self._cache_dir = cache_dir
        self._engine = engine
        self._speed = speed
        self._mlx_python = mlx_python
        self._kokoro_model = kokoro_model
        self._voicebox_config = voicebox_config
        self._volume = volume

        # Async infrastructure
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._orchestrator: Optional[TTSPipelineOrchestrator] = None
        self._running = False

        # Optional QueueManager interceptor (Wave 2.B). May be passed at
        # construction time or late-bound via `set_queue_manager`. Forwarded
        # to the orchestrator on `_start_orchestrator`.
        self._queue_manager: Optional["QueueManager"] = queue_manager

        # Sync text processor for direct use
        self._process_stage = ProcessStage(chunk_size=chunk_size)

        # Stats
        self._stats = {
            'submissions': 0,
            'completions': 0,
            'errors': 0,
        }

    def set_queue_manager(self, queue_manager: Optional["QueueManager"]) -> None:
        """Late-bind the QueueManager interceptor.

        If the orchestrator is already running, the binding is propagated to
        it immediately; otherwise the value is stashed and applied when
        `_start_orchestrator` runs.
        """
        self._queue_manager = queue_manager
        if self._orchestrator is not None:
            self._orchestrator.set_queue_manager(queue_manager)

    def start(self):
        """Start the async pipeline in a background thread."""
        if self._running:
            logger.warning("PipelineAdapter already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop,
            daemon=True,
            name="PipelineAdapter-EventLoop"
        )
        self._thread.start()

        # Wait for loop to be ready
        timeout = 5.0
        start = time.time()
        while self._loop is None and (time.time() - start) < timeout:
            time.sleep(0.01)

        if self._loop is None:
            raise RuntimeError("Failed to start event loop")

        # Start orchestrator in the event loop
        future = asyncio.run_coroutine_threadsafe(
            self._start_orchestrator(),
            self._loop
        )
        future.result(timeout=10.0)

        logger.info("PipelineAdapter started")

    def stop(self):
        """Stop the async pipeline gracefully."""
        if not self._running:
            return

        self._running = False

        if self._loop and self._orchestrator:
            future = asyncio.run_coroutine_threadsafe(
                self._orchestrator.stop(),
                self._loop
            )
            try:
                future.result(timeout=5.0)
            except Exception as e:
                logger.warning(f"Error stopping orchestrator: {e}")

        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=5.0)

        logger.info("PipelineAdapter stopped")

    def _run_event_loop(self):
        """Run the asyncio event loop in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
            self._loop = None

    async def _start_orchestrator(self):
        """Initialize and start the pipeline orchestrator."""
        self._orchestrator = TTSPipelineOrchestrator(
            workers_per_session=self._workers_per_session,
            chunk_size=self._chunk_size,
            buffer_size=self._buffer_size,
            cache_dir=self._cache_dir,
            voice=self._voice,
            queue_manager=self._queue_manager,
            engine=self._engine,
            speed=self._speed,
            mlx_python=self._mlx_python,
            kokoro_model=self._kokoro_model,
            voicebox_config=self._voicebox_config,
            volume=self._volume,
        )
        await self._orchestrator.start()

    def submit_async(
        self,
        content: str,
        session_id: Optional[str] = None,
        priority: int = 5,
        voice: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """
        Submit content to the async pipeline (non-blocking).

        This schedules the work but doesn't wait for completion.
        Use get_metrics() to track progress.

        Args:
            content: Text content to convert to speech
            session_id: Session identifier
            priority: Priority 1-10
            voice: TTS voice override
            request_id: OBSERVE-01 — caller-supplied UUID threaded
                through orchestrator → IngestMessage → AudioSegment for
                end-to-end log correlation. When None, the orchestrator
                falls back to a generated short-uuid.

        Returns:
            request_id for tracking
        """
        if not self._running or not self._loop or not self._orchestrator:
            raise RuntimeError("PipelineAdapter not running")

        future = asyncio.run_coroutine_threadsafe(
            self._orchestrator.submit(
                content, session_id, priority, voice, request_id=request_id,
            ),
            self._loop
        )

        try:
            returned_request_id = future.result(timeout=5.0)
            self._stats['submissions'] += 1
            return returned_request_id
        except Exception as e:
            self._stats['errors'] += 1
            logger.error(f"Failed to submit to pipeline: {e}")
            raise

    def submit_sync(
        self,
        content: str,
        session_id: Optional[str] = None,
        priority: int = 5,
        voice: Optional[str] = None,
        timeout: float = 30.0
    ) -> bool:
        """
        Submit content and wait for playback to complete (blocking).

        Returns:
            True if successfully played, False otherwise
        """
        if not self._running or not self._loop or not self._orchestrator:
            raise RuntimeError("PipelineAdapter not running")

        future = asyncio.run_coroutine_threadsafe(
            self._submit_and_wait(content, session_id, priority, voice),
            self._loop
        )

        try:
            return future.result(timeout=timeout)
        except Exception as e:
            self._stats['errors'] += 1
            logger.error(f"Failed to complete submission: {e}")
            return False

    async def _submit_and_wait(
        self,
        content: str,
        session_id: Optional[str],
        priority: int,
        voice: Optional[str]
    ) -> bool:
        """Submit and wait for completion."""
        try:
            request_id = await self._orchestrator.submit(
                content, session_id, priority, voice
            )

            # Wait for all segments to be played
            start_metrics = self._orchestrator.metrics.segments_played
            await asyncio.sleep(0.5)

            # Poll for completion (with timeout built into the orchestrator)
            max_wait = 60.0
            start_time = time.time()

            while (time.time() - start_time) < max_wait:
                metrics = self._orchestrator.get_metrics()
                generated = metrics['pipeline']['segments_generated']
                played = metrics['pipeline']['segments_played']

                if played >= generated and generated > 0:
                    self._stats['completions'] += 1
                    return True

                await asyncio.sleep(0.1)

            return False

        except Exception as e:
            logger.error(f"Error in submit_and_wait: {e}")
            return False

    def clean_text_for_speech(self, text: str) -> str:
        """
        Clean text using the new ProcessStage (sync interface).

        This provides the same contraction restoration and text cleaning
        as the full pipeline, but synchronously for direct use.
        """
        return self._process_stage._clean_text_sync(text)

    def restore_contractions(self, text: str) -> str:
        """
        Restore contractions only (sync interface).

        Converts expanded forms like "I am" → "I'm" for natural speech.
        """
        return self._process_stage._restore_contractions(text)

    def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive metrics from adapter and pipeline."""
        metrics = {
            'adapter': self._stats.copy(),
            'running': self._running,
        }

        if self._orchestrator:
            metrics['pipeline'] = self._orchestrator.get_metrics()

        return metrics

    def end_session(self, session_id: str):
        """End a specific session."""
        if not self._running or not self._loop or not self._orchestrator:
            return

        future = asyncio.run_coroutine_threadsafe(
            self._orchestrator.end_session(session_id),
            self._loop
        )
        try:
            future.result(timeout=5.0)
        except Exception as e:
            logger.warning(f"Error ending session {session_id}: {e}")


class SyncTextProcessor:
    """
    Standalone synchronous text processor.

    For use when you only need text cleaning/contraction restoration
    without the full async pipeline infrastructure.
    """

    def __init__(self, chunk_size: int = 150):
        self._stage = ProcessStage(chunk_size=chunk_size)

    def clean_text(self, text: str) -> str:
        """Clean text for speech (removes code, emojis, restores contractions)."""
        return self._stage._clean_text_sync(text)

    def restore_contractions(self, text: str) -> str:
        """Restore contractions only (I am → I'm)."""
        return self._stage._restore_contractions(text)

    def chunk_text(self, text: str) -> list[str]:
        """Split text into speakable chunks."""
        return self._stage._chunk_text(text)

    def process_full(self, text: str) -> tuple[str, list[str]]:
        """
        Full processing: clean text and chunk for TTS.

        Returns:
            Tuple of (cleaned_text, chunks)
        """
        cleaned = self.clean_text(text)
        chunks = self.chunk_text(cleaned)
        return cleaned, chunks


# Convenience singleton for daemon integration
_adapter_instance: Optional[PipelineAdapter] = None
_adapter_lock = threading.Lock()


def get_pipeline_adapter(
    voice: str = "en-US-AriaNeural",
    auto_start: bool = True
) -> PipelineAdapter:
    """
    Get or create the singleton pipeline adapter.

    Thread-safe singleton pattern for daemon integration.
    """
    global _adapter_instance

    with _adapter_lock:
        if _adapter_instance is None:
            _adapter_instance = PipelineAdapter(voice=voice)
            if auto_start:
                _adapter_instance.start()

        return _adapter_instance


def shutdown_pipeline_adapter():
    """Shutdown the singleton pipeline adapter."""
    global _adapter_instance

    with _adapter_lock:
        if _adapter_instance is not None:
            _adapter_instance.stop()
            _adapter_instance = None
