"""
Pipeline Orchestrator - Coordinates the complete TTS pipeline.

Connects all stages (Ingest → Process → Generate → Playback) and
manages session lifecycle, metrics, and graceful shutdown.

Wave 2.B addition: Optional `QueueManager` interceptor sits between
`ingest.consume` and `process.process`. When wired, every consumed
`IngestMessage` is passed through `qm.intercept(message, session_id)`
which may drop it (None), pass it through ([msg]), or expand to a
condensed/merged list ([m1, m2, ...]). The QueueManager is late-bound
(default None) so existing callers and tests remain backward-compatible.
"""

import asyncio
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, TYPE_CHECKING
import logging

from .ingest_stage import IngestStage, IngestMessage
from .process_stage import ProcessStage
from .generate_stage import GenerateStage
from .playback_stage import PlaybackStage

if TYPE_CHECKING:  # avoid runtime import cycle (queue_manager imports types only)
    from .queue_manager import QueueManager

logger = logging.getLogger(__name__)


@dataclass
class PipelineMetrics:
    """Real-time pipeline metrics."""
    messages_ingested: int = 0
    messages_processed: int = 0
    segments_generated: int = 0
    segments_played: int = 0
    avg_latency_ms: float = 0.0
    active_sessions: int = 0
    errors: int = 0

    # Latency tracking
    _latencies: list = field(default_factory=list)

    def record_latency(self, latency_ms: float):
        """Record a message latency measurement."""
        self._latencies.append(latency_ms)
        # Keep last 100 for moving average
        if len(self._latencies) > 100:
            self._latencies = self._latencies[-100:]
        self.avg_latency_ms = sum(self._latencies) / len(self._latencies)


class TTSPipelineOrchestrator:
    """
    Orchestrates the complete TTS pipeline.

    Coordinates all stages for optimal throughput and latency:
    - Event-driven ingest (zero polling)
    - Async text processing
    - Parallel TTS generation
    - Per-session playback
    """

    def __init__(
        self,
        workers_per_session: int = 3,
        chunk_size: int = 150,
        buffer_size: int = 3,
        cache_dir: str = "/tmp/tts_audio_cache",
        voice: str = "en-US-AriaNeural",
        queue_manager: Optional["QueueManager"] = None,
        engine: str = "edge-tts",
        speed: float = 1.0,
        mlx_python: Optional[str] = None,
        kokoro_model: Optional[str] = None,
        voicebox_config: Optional[dict] = None,
        volume: float = 1.0,
    ):
        # Initialize stages
        self.ingest = IngestStage()
        self.process = ProcessStage(chunk_size=chunk_size)
        self.generate = GenerateStage(
            workers_per_session=workers_per_session,
            cache_dir=cache_dir,
            voice=voice,
            engine=engine,
            speed=speed,
            mlx_python=mlx_python,
            kokoro_model=kokoro_model,
            voicebox_config=voicebox_config,
        )
        self.playback = PlaybackStage(buffer_size=buffer_size, volume=volume)

        # Optional drift-prevention interceptor. Late-binding via setter is
        # also supported (matches ContentRouter's late-bind pattern) so that
        # daemons can construct the orchestrator before the QueueManager is
        # ready, then wire it in at startup.
        self.queue_manager: Optional["QueueManager"] = queue_manager

        self.metrics = PipelineMetrics()
        self._running = False
        self._session_tasks: Dict[str, asyncio.Task] = {}
        self._config = {
            'workers_per_session': workers_per_session,
            'chunk_size': chunk_size,
            'buffer_size': buffer_size,
            'cache_dir': cache_dir,
            'voice': voice,
            'engine': engine,
            'speed': speed,
        }

    def set_queue_manager(self, queue_manager: Optional["QueueManager"]) -> None:
        """Late-bind the QueueManager interceptor.

        Mirrors `ContentRouter.set_queue_manager` so the daemon can wire
        components in any order. Passing ``None`` reverts to legacy behavior
        (every consumed message goes straight to ``process.process``).
        """
        self.queue_manager = queue_manager

    async def start(self):
        """Start the pipeline."""
        if self._running:
            logger.warning("Pipeline already running")
            return

        self._running = True
        await self.ingest.start()
        # Pre-warm the synthesis engine in the background so the user's first
        # utterance doesn't pay the one-time model-load/warmup cost. Fire-and-
        # forget: a warm failure is non-fatal (lazy retry on first synth).
        try:
            asyncio.create_task(self.generate.warm())
        except Exception as e:
            logger.debug(f"Could not schedule engine warm: {e}")
        # Periodically sweep orphaned cache files. _cleanup_session only deletes
        # a session's WAVs on explicit teardown, but long-lived / orphaned
        # sessions never send one, so /tmp/tts_audio_cache grows without bound.
        # That unbounded growth filled the system disk on 2026-06-21 and silently
        # muted ALL TTS engines (they could not write audio). This wires the
        # previously-dead cleanup_old_cache so the cache stays bounded.
        try:
            self._cache_sweep_task = asyncio.create_task(self._cache_sweep_loop())
        except Exception as e:
            logger.debug(f"Could not schedule cache sweep: {e}")
        logger.info("TTS Pipeline started")

    async def _cache_sweep_loop(self, interval_s: float = 600.0, max_age_s: int = 3600):
        """Bound the audio cache: every ``interval_s``, delete cached files older
        than ``max_age_s``. Playback is real-time (files are seconds old when
        played), so a 1h age floor never races an in-flight utterance. Never
        raises except on cancellation."""
        while self._running:
            try:
                await asyncio.sleep(interval_s)
                await self.generate.cleanup_old_cache(max_age_seconds=max_age_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
                logger.debug(f"cache sweep iteration failed: {e}")

    async def stop(self):
        """Stop the pipeline gracefully."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping TTS Pipeline...")

        # Stop the periodic cache-sweep loop.
        sweep = getattr(self, "_cache_sweep_task", None)
        if sweep is not None:
            sweep.cancel()
            try:
                await sweep
            except asyncio.CancelledError:
                pass
            self._cache_sweep_task = None

        # Stop ingest first (no new messages)
        await self.ingest.stop()

        # Cancel all session tasks
        for session_id, task in list(self._session_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._session_tasks.clear()

        # Cleanup all sessions
        for session_id in list(self.playback.session_buffers.keys()):
            await self.playback.stop_session(session_id)
            await self.generate.cleanup_session(session_id)
            await self.ingest.cleanup_session(session_id)

        logger.info("TTS Pipeline stopped")

    async def submit(
        self,
        content: str,
        session_id: Optional[str] = None,
        priority: int = 5,
        voice: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """
        Submit content to the pipeline.

        Args:
            content: Text content to convert to speech
            session_id: Session identifier (auto-generated if not provided)
            priority: Message priority (1-10, higher = more urgent)
            voice: Voice to use (defaults to pipeline default)
            request_id: OBSERVE-01 — caller-supplied request UUID for
                end-to-end traceability. When supplied, propagates as-is
                through IngestMessage → ProcessedMessage → AudioSegment so
                logs at every stage share the same identifier. When None,
                a short-uuid is generated as before (back-compat).

        Returns:
            request_id for tracking
        """
        if not self._running:
            raise RuntimeError("Pipeline not running. Call start() first.")

        # OBSERVE-01: honor caller-supplied request_id; fall back to
        # short-uuid for back-compat with internal/test callers.
        if not request_id:
            request_id = str(uuid.uuid4())[:8]
        if not session_id:
            session_id = str(uuid.uuid4())[:8]

        message = IngestMessage(
            content=content,
            session_id=session_id,
            priority=priority,
            request_id=request_id,
            ingested_at=time.time()
        )

        # Ingest
        success = await self.ingest.ingest(message)
        if not success:
            logger.warning(f"Failed to ingest message {request_id} (backpressure)")
            self.metrics.errors += 1
            return request_id

        self.metrics.messages_ingested += 1

        # Ensure session processing task is running
        if session_id not in self._session_tasks:
            task = asyncio.create_task(
                self._process_session(session_id, voice),
                name=f"session-proc[{session_id}]",
            )
            # DAEMON-04: log unhandled exceptions (PITFALLS N1/N6 mitigation)
            task.add_done_callback(
                lambda t, sid=session_id: (
                    logger.error("session-proc[%s] failed: %s", sid, t.exception())
                    if not t.cancelled() and t.done() and t.exception()
                    else None
                )
            )
            self._session_tasks[session_id] = task
            self.metrics.active_sessions += 1
            logger.debug(f"Started session processing for {session_id}")

        logger.debug(f"Submitted message {request_id} to session {session_id}")
        return request_id

    async def _process_session(
        self,
        session_id: str,
        voice: Optional[str] = None
    ):
        """
        Process messages for a session through the pipeline.

        Runs as a background task per session, consuming from ingest,
        processing, generating audio, and enqueueing for playback.
        """
        await self.playback.start_session(session_id)
        voice = voice or self._config['voice']

        while self._running:
            try:
                # 1. Consume from ingest (event-driven, no polling!)
                message = await self.ingest.consume(session_id, timeout=1.0)
                if not message:
                    continue

                start_time = time.time()

                # 1b. QueueManager interception (Wave 2.B).
                # When wired, the QueueManager inspects the message based on
                # current per-session lag tier and may:
                #   - drop it entirely (returns None) — TTL expired / redundant
                #   - condense to empty list — BLACK-tier "skipping N updates"
                #     where no items survive
                #   - pass through ([msg]) — GREEN tier, no change
                #   - replace/expand ([m1, m2, ...]) — coalesced or condensed
                # When QueueManager is None (legacy / pre-Wave-2 boot), the
                # message proceeds unchanged through the original pipeline.
                if self.queue_manager is not None:
                    replacements = await self.queue_manager.intercept(
                        message, session_id
                    )
                    if replacements is None:
                        # Silently dropped (TTL, redundant, low-pri RED-drop).
                        continue
                    if not replacements:
                        # Condensed-to-empty (BLACK with no surviving items).
                        continue
                    msgs_to_process = replacements
                else:
                    msgs_to_process = [message]

                # 2-4. Process each (post-intercept) message through the rest
                # of the pipeline. A single intercept call may have produced a
                # condensed/merged batch, so we iterate.
                for msg in msgs_to_process:
                    # 2. Process text (async, thread pool for CPU work)
                    processed = await self.process.process(msg)
                    self.metrics.messages_processed += 1

                    # 3. Generate audio (parallel workers)
                    async for segment in self.generate.generate(processed, voice):
                        self.metrics.segments_generated += 1

                        # 4. Enqueue for playback (per-session buffer)
                        await self.playback.enqueue_segment(segment)
                        self.metrics.segments_played += 1

                # Record end-to-end latency for the originating ingest event.
                latency_ms = (time.time() - start_time) * 1000
                self.metrics.record_latency(latency_ms)

                logger.debug(
                    f"Processed message {message.request_id} in {latency_ms:.0f}ms "
                    f"(produced {len(msgs_to_process)} downstream message(s))"
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing session {session_id}: {e}")
                self.metrics.errors += 1
                continue

        # Cleanup on exit
        await self.playback.stop_session(session_id)
        await self.generate.cleanup_session(session_id)
        await self.ingest.cleanup_session(session_id)
        self.metrics.active_sessions -= 1

        if session_id in self._session_tasks:
            del self._session_tasks[session_id]

        logger.debug(f"Session {session_id} processing ended")

    async def end_session(self, session_id: str):
        """Explicitly end a session and cleanup resources."""
        if session_id in self._session_tasks:
            self._session_tasks[session_id].cancel()
            try:
                await self._session_tasks[session_id]
            except asyncio.CancelledError:
                pass
            del self._session_tasks[session_id]

        await self.playback.stop_session(session_id)
        await self.generate.cleanup_session(session_id)
        await self.ingest.cleanup_session(session_id)
        self.metrics.active_sessions = max(0, self.metrics.active_sessions - 1)

    def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive pipeline metrics."""
        return {
            'pipeline': {
                'messages_ingested': self.metrics.messages_ingested,
                'messages_processed': self.metrics.messages_processed,
                'segments_generated': self.metrics.segments_generated,
                'segments_played': self.metrics.segments_played,
                'avg_latency_ms': round(self.metrics.avg_latency_ms, 1),
                'active_sessions': self.metrics.active_sessions,
                'errors': self.metrics.errors,
            },
            'ingest': self.ingest.get_stats(),
            'process': self.process.get_stats(),
            'generate': self.generate.get_stats(),
            'playback': self.playback.get_stats(),
        }

    def get_session_status(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get status for a specific session."""
        playback_state = self.playback.get_session_state(session_id)
        if not playback_state:
            return None

        return {
            'session_id': session_id,
            'is_playing': playback_state.is_playing,
            'segments_played': playback_state.segments_played,
            'total_duration_ms': playback_state.total_duration_ms,
            'errors': playback_state.errors,
            'is_active': session_id in self._session_tasks,
        }


# Convenience function for simple usage
async def speak(text: str, voice: str = "en-US-AriaNeural") -> str:
    """
    Simple function to speak text using the pipeline.

    Creates a temporary pipeline, speaks the text, and cleans up.
    For production use, create a persistent TTSPipelineOrchestrator.

    Args:
        text: Text to speak
        voice: Voice to use

    Returns:
        request_id
    """
    orchestrator = TTSPipelineOrchestrator(voice=voice)
    await orchestrator.start()

    try:
        request_id = await orchestrator.submit(text)
        # Wait for playback to complete
        await asyncio.sleep(0.5)  # Initial delay
        while orchestrator.metrics.segments_played < orchestrator.metrics.segments_generated:
            await asyncio.sleep(0.1)
        return request_id
    finally:
        await orchestrator.stop()
