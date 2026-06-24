"""TTSEngine — synthesize one chunk of text to an audio file.

Implementations write `out_path` and return True on success, False on failure
(GenerateStage treats False as a generation error and drops the chunk). Kokoro
(daemon/pipeline/kokoro_engine.py) already conforms structurally; EdgeTTSEngine
is the new sibling. Voicebox is NOT a TTSEngine — it offloads synthesis AND
playback and yields no segments (handled at GenerateStage.generate()).
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class TTSEngine(ABC):
    @abstractmethod
    async def synthesize(
        self, text: str, out_path: str, voice: str, speed: float = 1.0
    ) -> bool:
        """Write synthesized audio for `text` to `out_path`. Return success."""
        raise NotImplementedError
