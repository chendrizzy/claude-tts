"""EdgeTTSEngine — Azure network voices via the edge-tts library."""
from __future__ import annotations

import logging

from daemon.engines.base import TTSEngine

logger = logging.getLogger(__name__)


class EdgeTTSEngine(TTSEngine):
    def __init__(self) -> None:
        self._mod = None  # lazily imported edge_tts module

    def _module(self):
        if self._mod is None:
            try:
                import edge_tts
            except ImportError:
                logger.error("edge_tts not installed. Run: pip install edge-tts")
                raise
            self._mod = edge_tts
        return self._mod

    async def synthesize(
        self, text: str, out_path: str, voice: str, speed: float = 1.0
    ) -> bool:
        # edge-tts (Azure) exposes no speed control — `speed` is ignored, matching
        # the prior inline behavior.
        try:
            communicate = self._module().Communicate(text, voice)
            await communicate.save(out_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("edge-tts synth failed: %s", exc)
            return False
