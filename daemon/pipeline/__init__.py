"""
TTS Pipeline Module - Event-driven, async TTS processing pipeline.

This module implements a high-performance TTS pipeline with:
- Event-driven message ingest (zero-polling latency)
- Async text preprocessing with thread pool
- Parallel TTS generation workers
- Per-session audio playback (no global lock)
"""

from .ingest_stage import IngestStage, IngestMessage
from .process_stage import ProcessStage, ProcessedMessage
from .generate_stage import GenerateStage, AudioSegment
from .playback_stage import PlaybackStage, PlaybackState
from .orchestrator import TTSPipelineOrchestrator, PipelineMetrics
from .adapter import (
    PipelineAdapter,
    SyncTextProcessor,
    get_pipeline_adapter,
    shutdown_pipeline_adapter,
)

__all__ = [
    # Core stages
    'IngestStage',
    'IngestMessage',
    'ProcessStage',
    'ProcessedMessage',
    'GenerateStage',
    'AudioSegment',
    'PlaybackStage',
    'PlaybackState',
    # Orchestrator
    'TTSPipelineOrchestrator',
    'PipelineMetrics',
    # Adapter for daemon integration
    'PipelineAdapter',
    'SyncTextProcessor',
    'get_pipeline_adapter',
    'shutdown_pipeline_adapter',
]

__version__ = '1.0.0'
