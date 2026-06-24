"""Unit tests for the TTSEngine synthesis seam."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from daemon.engines.base import TTSEngine
from daemon.engines.edge_tts_engine import EdgeTTSEngine


def test_ttsengine_is_abstract():
    with pytest.raises(TypeError):
        TTSEngine()


def test_ttsengine_declares_synthesize():
    assert hasattr(TTSEngine, "synthesize")


class _FakeComm:
    def __init__(self, text, voice):
        self.text, self.voice = text, voice

    async def save(self, path):
        Path(path).write_bytes(b"audio")


class _FakeEdge:
    Communicate = _FakeComm


def test_edge_synthesize_writes_file_and_returns_true(tmp_path):
    e = EdgeTTSEngine()
    e._mod = _FakeEdge()  # inject fake module (skip real edge-tts import)
    out = tmp_path / "a.mp3"
    ok = asyncio.run(e.synthesize("hi", str(out), "en-US-AvaNeural", 1.0))
    assert ok is True
    assert out.exists()


def test_edge_synthesize_returns_false_on_error(tmp_path):
    class _Boom:
        class Communicate:
            def __init__(self, *a):
                raise RuntimeError("boom")

    e = EdgeTTSEngine()
    e._mod = _Boom()
    ok = asyncio.run(e.synthesize("hi", str(tmp_path / "b.mp3"), "v", 1.0))
    assert ok is False
