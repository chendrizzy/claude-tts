"""TTS synthesis engines behind a common interface."""
from daemon.engines.base import TTSEngine
from daemon.engines.edge_tts_engine import EdgeTTSEngine
from daemon.engines.system_tts_engine import SystemTTSEngine
from daemon.engines.factory import make_engine

__all__ = ["TTSEngine", "EdgeTTSEngine", "SystemTTSEngine", "make_engine"]
