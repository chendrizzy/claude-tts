"""Engine factory — selects a stateless TTSEngine by config name.

Mirrors ``daemon.providers.make_provider``. Covers the synchronous, no-arg
engines (``edge-tts``/``say``/``espeak``). Kokoro is intentionally NOT handled
here: it owns a persistent subprocess worker with an async start, so it keeps
its own lazy getter in GenerateStage. Voicebox is not a TTSEngine at all (it
offloads synthesis+playback and bypasses GenerateStage).
"""
from __future__ import annotations

from daemon.engines.base import TTSEngine
from daemon.engines.edge_tts_engine import EdgeTTSEngine
from daemon.engines.system_tts_engine import SystemTTSEngine


def make_engine(engine: str) -> TTSEngine:
    """Return a fresh TTSEngine for ``engine`` (a config name).

    ``say``/``espeak``/``system`` → SystemTTSEngine (zero-dep OS speech).
    Anything else → EdgeTTSEngine (the default, and the safety net for
    unknown names — never raises, matching make_provider).
    """
    name = str(engine or "").lower()
    if name in ("say", "espeak", "system"):
        return SystemTTSEngine()
    return EdgeTTSEngine()
