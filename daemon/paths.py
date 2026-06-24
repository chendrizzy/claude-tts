"""Portable path resolution for claude-tts.

Single source of truth for the daemon socket and the user config location.
Every function is pure and env-driven so the POSIX-sh hooks can mirror the
same resolution with inline ``${VAR:-default}`` expansions, guaranteeing the
daemon and its hooks always agree on where the socket and config live.

Precedence (highest first):
    config_dir  : $XDG_CONFIG_HOME/claude-tts  ->  ~/.config/claude-tts
    config_path : $CLAUDE_TTS_CONFIG           ->  config_dir()/config.json
    socket_path : $CLAUDE_TTS_SOCKET           ->  ${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock
"""
import os
from pathlib import Path


def config_dir() -> Path:
    """Directory holding the claude-tts user config.

    Honors $XDG_CONFIG_HOME; otherwise ~/.config/claude-tts.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "claude-tts"


def config_path() -> Path:
    """Full path to the user config file.

    Honors $CLAUDE_TTS_CONFIG; otherwise config_dir()/config.json.
    """
    override = os.environ.get("CLAUDE_TTS_CONFIG")
    if override:
        return Path(override)
    return config_dir() / "config.json"


def socket_path() -> str:
    """Unix-domain socket path the daemon binds and the hooks probe.

    Honors $CLAUDE_TTS_SOCKET; otherwise ${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock.
    Returned as ``str`` to match the existing SOCKET_PATH constant's type.
    """
    override = os.environ.get("CLAUDE_TTS_SOCKET")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return f"{runtime}/claude-tts.sock"
