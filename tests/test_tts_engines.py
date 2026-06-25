"""Unit tests for the TTSEngine synthesis seam."""
import asyncio
import subprocess
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


# --- SystemTTSEngine (say/espeak zero-dep fallback) ---
import platform as _platform_mod
import shutil as _shutil_mod
from daemon.engines.system_tts_engine import SystemTTSEngine


class _FakeProc:
    """Stand-in for an asyncio subprocess: optionally 'writes' the output file."""

    def __init__(self, rc, out_path, write=True, content=b"RIFF\x00\x00\x00\x00WAVEfakebytes"):
        self._rc, self._out, self._write, self._content = rc, out_path, write, content

    async def wait(self):
        if self._write:
            Path(self._out).write_bytes(self._content)
        return self._rc


def _fake_exec_factory(captured, rc=0, write=True, flag="-o", content=b"RIFF\x00\x00\x00\x00WAVEfakebytes"):
    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        out = cmd[cmd.index(flag) + 1]
        return _FakeProc(rc, out, write=write, content=content)
    return fake_exec


def test_system_synthesize_darwin_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Darwin")
    captured = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(captured))
    e = SystemTTSEngine()
    out = tmp_path / "a.wav"
    ok = asyncio.run(e.synthesize("hi there", str(out), "ignored-voice", 1.0))
    assert ok is True
    assert out.exists()
    assert captured["cmd"][0] == "say"
    assert "hi there" in captured["cmd"]  # text passed as exec arg (no shell)
    assert "--file-format=WAVE" in captured["cmd"]
    assert "--data-format=LEI16@22050" in captured["cmd"]


def test_system_synthesize_linux_uses_espeak(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Linux")
    monkeypatch.setattr(_shutil_mod, "which",
                        lambda b: "/usr/bin/espeak" if b == "espeak" else None)
    captured = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(captured, flag="-w"))
    e = SystemTTSEngine()
    out = tmp_path / "a.wav"
    ok = asyncio.run(e.synthesize("hi", str(out), "v", 1.0))
    assert ok is True
    assert captured["cmd"][0] == "/usr/bin/espeak"


def test_system_synthesize_no_binary_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Linux")
    monkeypatch.setattr(_shutil_mod, "which", lambda b: None)
    e = SystemTTSEngine()
    ok = asyncio.run(e.synthesize("hi", str(tmp_path / "a.wav"), "v", 1.0))
    assert ok is False


def test_system_synthesize_nonzero_rc_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Darwin")
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory({}, rc=1, write=False))
    e = SystemTTSEngine()
    ok = asyncio.run(e.synthesize("hi", str(tmp_path / "a.wav"), "v", 1.0))
    assert ok is False


def test_system_speed_maps_to_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Darwin")
    captured = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(captured))
    e = SystemTTSEngine()
    asyncio.run(e.synthesize("hi", str(tmp_path / "a.wav"), "v", 2.0))
    assert "-r" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-r") + 1] == "350"  # 175 * 2.0


# --- make_engine factory (mirrors make_provider) ---
from daemon.engines import make_engine


def test_make_engine_say_returns_system():
    from daemon.engines import SystemTTSEngine
    assert isinstance(make_engine("say"), SystemTTSEngine)


def test_make_engine_espeak_returns_system():
    from daemon.engines import SystemTTSEngine
    assert isinstance(make_engine("espeak"), SystemTTSEngine)


def test_make_engine_system_alias_returns_system():
    from daemon.engines import SystemTTSEngine
    assert isinstance(make_engine("system"), SystemTTSEngine)


# --- GenerateStage routes engine selection through make_engine ---
def test_generate_stage_get_engine_say_returns_system():
    from daemon.pipeline.generate_stage import GenerateStage
    from daemon.engines import SystemTTSEngine
    stage = GenerateStage.__new__(GenerateStage)  # bypass __init__
    stage.engine = "say"
    stage._engine = None
    assert isinstance(stage._get_engine(), SystemTTSEngine)


def test_generate_stage_get_engine_defaults_to_edge():
    from daemon.pipeline.generate_stage import GenerateStage
    from daemon.engines import EdgeTTSEngine
    stage = GenerateStage.__new__(GenerateStage)
    stage.engine = "edge-tts"
    stage._engine = None
    assert isinstance(stage._get_engine(), EdgeTTSEngine)


def test_generate_stage_get_engine_is_cached():
    from daemon.pipeline.generate_stage import GenerateStage
    stage = GenerateStage.__new__(GenerateStage)
    stage.engine = "say"
    stage._engine = None
    first = stage._get_engine()
    assert stage._get_engine() is first  # lazy + cached, like _get_edge_engine was


def test_make_engine_case_insensitive():
    from daemon.engines import SystemTTSEngine
    assert isinstance(make_engine("SAY"), SystemTTSEngine)


def test_make_engine_edge_returns_edge():
    assert isinstance(make_engine("edge-tts"), EdgeTTSEngine)


def test_make_engine_unknown_defaults_to_edge():
    # default safety net — mirrors make_provider's "default: ollama" branch.
    assert isinstance(make_engine("totally-unknown"), EdgeTTSEngine)
    assert isinstance(make_engine(""), EdgeTTSEngine)


# --- FIX 1: _audio_ext_for helper ---
def test_audio_ext_wav_for_system_engines():
    from daemon.pipeline.generate_stage import _audio_ext_for
    for eng in ("say", "espeak", "system", "kokoro", "mlx-audio"):
        assert _audio_ext_for(eng) == "wav", eng
    for eng in ("edge-tts", "voicebox", "", "unknown"):
        assert _audio_ext_for(eng) == "mp3", eng
    assert _audio_ext_for("SAY") == "wav"  # case-insensitive


# --- FIX 2: -- end-of-options separator ---
def test_system_dash_text_passed_after_separator(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Darwin")
    captured = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec_factory(captured))
    e = SystemTTSEngine()
    asyncio.run(e.synthesize("--version is the flag", str(tmp_path / "a.wav"), "", 1.0))
    cmd = captured["cmd"]
    assert "--" in cmd
    assert cmd[-1] == "--version is the flag"      # text is last
    assert cmd[cmd.index("--") + 1] == "--version is the flag"  # text immediately follows the separator


# --- FIX 4a: zero-byte guard ---
def test_system_synthesize_empty_output_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Darwin")
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory({}, rc=0, write=True, content=b""))
    e = SystemTTSEngine()
    ok = asyncio.run(e.synthesize("hi", str(tmp_path / "a.wav"), "v", 1.0))
    assert ok is False  # rc==0 but 0-byte file → not success


# --- FIX 4c: espeak speed mapping ---
def test_system_espeak_speed_maps_to_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(_platform_mod, "system", lambda: "Linux")
    monkeypatch.setattr(_shutil_mod, "which",
                        lambda b: "/usr/bin/espeak" if b == "espeak" else None)
    captured = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(captured, flag="-w"))
    e = SystemTTSEngine()
    asyncio.run(e.synthesize("hi", str(tmp_path / "a.wav"), "v", 2.0))
    assert "-s" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-s") + 1] == "350"  # 175 * 2.0


# --- FIX 4d: real macOS say integration test ---
@pytest.mark.skipif(
    _platform_mod.system() != "Darwin" or _shutil_mod.which("say") is None
    or _shutil_mod.which("afinfo") is None,
    reason="needs macOS say + afinfo",
)
def test_system_real_say_writes_afplay_openable_wav(tmp_path):
    out = tmp_path / "real.wav"
    e = SystemTTSEngine()
    ok = asyncio.run(e.synthesize("plan 3d regression check", str(out), "", 1.0))
    assert ok is True
    data = out.read_bytes()
    assert data[:4] == b"RIFF" and b"WAVE" in data[:16]
    # afinfo opens via AudioToolbox exactly like afplay — rc 0 proves the file
    # is a real, openable WAV. This is the check that catches the .mp3-extension blocker.
    rc = subprocess.run(["afinfo", str(out)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    assert rc == 0
