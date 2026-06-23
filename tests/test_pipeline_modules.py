"""
Comprehensive tests for the new TTS pipeline modules.

Tests cover:
- IngestStage: Event-driven message ingest
- ProcessStage: Text cleaning and contraction restoration
- GenerateStage: Parallel TTS generation
- PlaybackStage: Per-session audio playback
- Orchestrator: Pipeline coordination
- Adapter: Sync/async bridge
"""

import asyncio
import pytest
import time
import os
import sys
import tempfile

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon.pipeline import (
    IngestStage,
    IngestMessage,
    ProcessStage,
    ProcessedMessage,
    GenerateStage,
    AudioSegment,
    PlaybackStage,
    PlaybackState,
    TTSPipelineOrchestrator,
    PipelineMetrics,
    SyncTextProcessor,
)


class TestIngestStage:
    """Tests for event-driven message ingest."""

    @pytest.fixture
    def ingest(self):
        return IngestStage()

    @pytest.mark.asyncio
    async def test_start_stop(self, ingest):
        """Test stage lifecycle."""
        await ingest.start()
        assert ingest._running is True

        await ingest.stop()
        assert ingest._running is False

    @pytest.mark.asyncio
    async def test_ingest_message(self, ingest):
        """Test message ingestion."""
        await ingest.start()

        message = IngestMessage(
            content="Hello world",
            session_id="test-session",
            priority=5,
            request_id="req-001",
            ingested_at=time.time()
        )

        success = await ingest.ingest(message)
        assert success is True
        assert ingest._stats['messages_ingested'] == 1

        await ingest.stop()

    @pytest.mark.asyncio
    async def test_consume_message(self, ingest):
        """Test message consumption with event notification."""
        await ingest.start()

        message = IngestMessage(
            content="Test content",
            session_id="test-session",
            priority=5,
            request_id="req-002",
            ingested_at=time.time()
        )

        await ingest.ingest(message)

        # Consume should return immediately (event-driven)
        consumed = await ingest.consume("test-session", timeout=1.0)
        assert consumed is not None
        assert consumed.content == "Test content"
        assert ingest._stats['messages_consumed'] == 1

        await ingest.stop()

    @pytest.mark.asyncio
    async def test_consume_timeout(self, ingest):
        """Test consume timeout when no message available."""
        await ingest.start()

        start = time.time()
        consumed = await ingest.consume("nonexistent-session", timeout=0.1)
        elapsed = time.time() - start

        assert consumed is None
        assert elapsed < 0.3  # Should timeout quickly

        await ingest.stop()

    @pytest.mark.asyncio
    async def test_backpressure(self, ingest):
        """Test backpressure when queue is full."""
        # Create ingest with small queue
        small_ingest = IngestStage(max_queue_size=10)
        await small_ingest.start()

        # Fill the session queue (default session queue size is 100, not max_queue_size)
        # We need to check the session queue specifically
        session_id = "backpressure-test"
        for i in range(100):
            message = IngestMessage(
                content=f"Message {i}",
                session_id=session_id,
                priority=5,
                request_id=f"req-{i}",
                ingested_at=time.time()
            )
            await small_ingest.ingest(message)

        # Next message should trigger backpressure
        overflow = IngestMessage(
            content="Overflow message",
            session_id=session_id,
            priority=5,
            request_id="overflow",
            ingested_at=time.time()
        )
        result = await small_ingest.ingest(overflow)
        assert result is False
        assert small_ingest._stats['backpressure_events'] > 0

        await small_ingest.stop()


class TestProcessStage:
    """Tests for text processing and contraction restoration."""

    @pytest.fixture
    def process(self):
        return ProcessStage(chunk_size=150)

    def test_contraction_restoration_basic(self, process):
        """Test basic contraction restoration."""
        test_cases = [
            ("I am happy", "I'm happy"),
            ("you are welcome", "you're welcome"),
            ("it is working", "it's working"),
            ("we have finished", "we've finished"),
            ("they will come", "they'll come"),
            ("I would like that", "I'd like that"),
        ]

        for input_text, expected in test_cases:
            result = process._restore_contractions(input_text)
            assert result == expected, f"Failed: '{input_text}' → '{result}' (expected '{expected}')"

    def test_contraction_restoration_negatives(self, process):
        """Test negative contraction restoration."""
        test_cases = [
            ("do not worry", "don't worry"),
            ("does not work", "doesn't work"),
            ("did not see", "didn't see"),
            ("will not go", "won't go"),
            ("cannot help", "can't help"),
            ("is not ready", "isn't ready"),
            ("are not here", "aren't here"),
            ("was not there", "wasn't there"),
            ("were not sure", "weren't sure"),
            ("has not arrived", "hasn't arrived"),
            ("have not tried", "haven't tried"),
            ("had not known", "hadn't known"),
        ]

        for input_text, expected in test_cases:
            result = process._restore_contractions(input_text)
            assert result == expected, f"Failed: '{input_text}' → '{result}' (expected '{expected}')"

    def test_contraction_preservation(self, process):
        """Test that existing contractions are preserved (not expanded!)."""
        # CRITICAL: Contractions must NEVER be expanded
        test_cases = [
            "I'm happy",
            "you're welcome",
            "it's working",
            "we've finished",
            "they'll come",
            "don't worry",
            "can't help",
            "isn't ready",
            "what's up",
        ]

        for text in test_cases:
            result = process._restore_contractions(text)
            assert result == text, f"Contraction was modified: '{text}' → '{result}'"

    def test_capitalization_preserved(self, process):
        """Test that capitalization is preserved during contraction."""
        test_cases = [
            ("I am happy", "I'm happy"),
            ("DO NOT PANIC", "DON'T PANIC"),
            ("He is here", "He's here"),
        ]

        for input_text, expected in test_cases:
            result = process._restore_contractions(input_text)
            assert result == expected, f"Failed: '{input_text}' → '{result}' (expected '{expected}')"

    def test_clean_text_removes_code_blocks(self, process):
        """Test that code blocks are removed."""
        text = "Here is code: ```python\nprint('hello')\n``` done"
        result = process._clean_text_sync(text)
        assert "```" not in result
        assert "print" not in result

    def test_clean_text_removes_emojis(self, process):
        """Test that emojis are removed."""
        text = "Hello 👋 world 🌍!"
        result = process._clean_text_sync(text)
        assert "👋" not in result
        assert "🌍" not in result
        assert "Hello" in result
        assert "world" in result

    def test_clean_text_full_pipeline(self, process):
        """Test full text cleaning pipeline."""
        text = "I am going to ```code``` 🎉 show you how it is done!"
        result = process._clean_text_sync(text)

        # Should contract expanded forms
        assert "I'm" in result
        # Should remove code blocks
        assert "```" not in result
        # Should remove emojis
        assert "🎉" not in result
        # Should contract "it is"
        assert "it's" in result

    def test_chunking_basic(self, process):
        """Test text chunking for streaming TTS."""
        short_text = "Hello world."
        chunks = process._chunk_text(short_text)
        assert len(chunks) == 1
        assert chunks[0] == short_text

    def test_chunking_long_text(self, process):
        """Test chunking of long text."""
        long_text = " ".join(["This is a sentence."] * 20)
        chunks = process._chunk_text(long_text)

        assert len(chunks) > 1
        # Each chunk should be under the limit
        for chunk in chunks:
            assert len(chunk) <= 200  # Some buffer above chunk_size

    @pytest.mark.asyncio
    async def test_process_message(self, process):
        """Test async message processing."""
        message = IngestMessage(
            content="I am going to show you what is possible. Do not worry about it.",
            session_id="test",
            priority=5,
            request_id="req-001",
            ingested_at=time.time()
        )

        result = await process.process(message)

        assert isinstance(result, ProcessedMessage)
        assert "I'm" in result.cleaned
        assert "what's" in result.cleaned
        assert "Don't" in result.cleaned
        assert len(result.chunks) >= 1


class TestSyncTextProcessor:
    """Tests for the standalone sync text processor."""

    @pytest.fixture
    def processor(self):
        return SyncTextProcessor()

    def test_clean_text(self, processor):
        """Test sync text cleaning."""
        text = "I am going to 🎉 help you!"
        result = processor.clean_text(text)

        assert "I'm" in result
        assert "🎉" not in result

    def test_restore_contractions(self, processor):
        """Test sync contraction restoration."""
        text = "What is your name? I am Claude."
        result = processor.restore_contractions(text)

        assert "What's" in result
        assert "I'm" in result

    def test_chunk_text(self, processor):
        """Test sync text chunking."""
        text = "This is a test. " * 15
        chunks = processor.chunk_text(text)

        assert len(chunks) > 1

    def test_process_full(self, processor):
        """Test full sync processing."""
        text = "I am going to ```code``` show you. Do not worry."
        cleaned, chunks = processor.process_full(text)

        assert "I'm" in cleaned
        assert "Don't" in cleaned
        assert "```" not in cleaned
        assert len(chunks) >= 1


class TestGenerateStage:
    """Tests for parallel TTS generation."""

    @pytest.fixture
    def generate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield GenerateStage(
                workers_per_session=2,
                cache_dir=tmpdir,
                voice="en-US-AriaNeural"
            )

    def test_cache_directory_created(self, generate):
        """Test that cache directory is created."""
        assert os.path.exists(generate.cache_dir)

    def test_stats_initialization(self, generate):
        """Test initial stats."""
        stats = generate.get_stats()
        assert stats['segments_generated'] == 0
        assert stats['cache_hits'] == 0
        assert stats['generation_errors'] == 0


class TestPlaybackStage:
    """Tests for per-session audio playback."""

    @pytest.fixture
    def playback(self):
        return PlaybackStage(buffer_size=3, crossfade_ms=50)

    @pytest.mark.asyncio
    async def test_start_session(self, playback):
        """Test session initialization."""
        session_id = "test-session"
        await playback.start_session(session_id)

        assert session_id in playback.session_buffers
        assert session_id in playback.session_locks
        assert session_id in playback.session_states
        assert session_id in playback._playback_tasks

        await playback.stop_session(session_id)

    @pytest.mark.asyncio
    async def test_stop_session(self, playback):
        """Test session cleanup."""
        session_id = "test-session"
        await playback.start_session(session_id)
        await playback.stop_session(session_id)

        assert session_id not in playback.session_buffers
        assert session_id not in playback.session_locks
        assert session_id not in playback.session_states

    @pytest.mark.asyncio
    async def test_per_session_locks(self, playback):
        """Test that sessions have independent locks."""
        session1 = "session-1"
        session2 = "session-2"

        await playback.start_session(session1)
        await playback.start_session(session2)

        # Each session should have its own lock
        lock1 = playback.session_locks[session1]
        lock2 = playback.session_locks[session2]

        assert lock1 is not lock2

        await playback.stop_session(session1)
        await playback.stop_session(session2)

    def test_stats(self, playback):
        """Test playback stats."""
        stats = playback.get_stats()
        assert 'segments_played' in stats
        assert 'playback_errors' in stats
        assert 'active_sessions' in stats


class TestOrchestrator:
    """Tests for pipeline orchestration."""

    @pytest.fixture
    def orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield TTSPipelineOrchestrator(
                workers_per_session=2,
                chunk_size=150,
                buffer_size=2,
                cache_dir=tmpdir,
                voice="en-US-AriaNeural"
            )

    @pytest.mark.asyncio
    async def test_start_stop(self, orchestrator):
        """Test orchestrator lifecycle."""
        await orchestrator.start()
        assert orchestrator._running is True

        await orchestrator.stop()
        assert orchestrator._running is False

    @pytest.mark.asyncio
    async def test_metrics(self, orchestrator):
        """Test metrics collection."""
        await orchestrator.start()

        metrics = orchestrator.get_metrics()
        assert 'pipeline' in metrics
        assert 'ingest' in metrics
        assert 'process' in metrics
        assert 'generate' in metrics
        assert 'playback' in metrics

        await orchestrator.stop()


class TestContractionRegressions:
    """
    CRITICAL: Regression tests for contraction handling.

    These tests ensure contractions are NEVER expanded.
    Any failure here indicates a serious regression.
    """

    @pytest.fixture
    def processor(self):
        return SyncTextProcessor()

    def test_im_never_expanded(self, processor):
        """CRITICAL: 'I'm' must NEVER become 'I am'."""
        text = "I'm happy today"
        result = processor.clean_text(text)
        assert "I'm" in result
        assert "I am" not in result

    def test_whats_never_expanded(self, processor):
        """CRITICAL: 'what's' must NEVER become 'what is'."""
        text = "What's going on?"
        result = processor.clean_text(text)
        assert "What's" in result
        assert "What is" not in result

    def test_weve_never_expanded(self, processor):
        """CRITICAL: 'we've' must NEVER become 'we have'."""
        text = "We've been waiting"
        result = processor.clean_text(text)
        assert "We've" in result
        assert "We have" not in result

    def test_dont_never_expanded(self, processor):
        """CRITICAL: 'don't' must NEVER become 'do not'."""
        text = "Don't worry about it"
        result = processor.clean_text(text)
        assert "Don't" in result
        assert "Do not" not in result

    def test_cant_never_expanded(self, processor):
        """CRITICAL: 'can't' must NEVER become 'cannot'."""
        text = "I can't believe it"
        result = processor.clean_text(text)
        assert "can't" in result
        assert "cannot" not in result.lower()

    def test_mixed_contractions_preserved(self, processor):
        """Test mixed text with existing contractions."""
        text = "I'm sure that you're right. We've done this before, haven't we?"
        result = processor.clean_text(text)

        # All contractions must remain
        assert "I'm" in result
        assert "you're" in result
        assert "We've" in result
        assert "haven't" in result

        # No expansions
        assert "I am" not in result
        assert "you are" not in result
        assert "We have" not in result
        assert "have not" not in result

    def test_expanded_forms_contracted(self, processor):
        """Test that expanded forms ARE contracted."""
        text = "I am sure that you are right. We have done this before."
        result = processor.clean_text(text)

        # Expanded forms should be contracted
        assert "I'm" in result
        assert "you're" in result
        assert "We've" in result


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
