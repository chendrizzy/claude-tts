"""TTS synthesis engines behind a common interface."""
from daemon.engines.base import TTSEngine
from daemon.engines.edge_tts_engine import EdgeTTSEngine

__all__ = ["TTSEngine", "EdgeTTSEngine"]
