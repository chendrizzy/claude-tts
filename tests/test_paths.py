"""Unit tests for the portable path seam (daemon/paths.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.paths import config_dir, config_path, socket_path


def test_config_dir_honors_xdg_config_home(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    assert config_dir() == Path("/xdg/cfg/claude-tts")


def test_config_dir_defaults_to_home_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    assert config_dir() == Path("/home/tester/.config/claude-tts")


def test_config_path_honors_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_TTS_CONFIG", "/custom/place/cfg.json")
    assert config_path() == Path("/custom/place/cfg.json")


def test_config_path_defaults_to_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_TTS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    assert config_path() == Path("/xdg/cfg/claude-tts/config.json")


def test_socket_path_honors_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_TTS_SOCKET", "/run/custom.sock")
    assert socket_path() == "/run/custom.sock"


def test_socket_path_uses_xdg_runtime_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_TTS_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert socket_path() == "/run/user/1000/claude-tts.sock"


def test_socket_path_defaults_to_tmp(monkeypatch):
    monkeypatch.delenv("CLAUDE_TTS_SOCKET", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert socket_path() == "/tmp/claude-tts.sock"
