"""Unit tests for the Platform audio-playback seam."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from daemon.platforms.base import (
    Platform, PlatformMacOS, PlatformLinux, PlatformWindows, make_platform,
)


def test_platform_is_abstract():
    with pytest.raises(TypeError):
        Platform()


def test_macos_cmd_with_volume():
    assert PlatformMacOS().build_player_cmd("/a.wav", 3.5) == ["afplay", "-v", "3.500", "/a.wav"]


def test_macos_cmd_unit_volume_omits_flag():
    assert PlatformMacOS().build_player_cmd("/a.wav", 1.0) == ["afplay", "/a.wav"]


def test_linux_cmd():
    assert PlatformLinux().build_player_cmd("/a.wav", 2.0) == [
        "mpv", "--no-video", "--really-quiet", "/a.wav",
    ]


def test_windows_cmd():
    assert PlatformWindows().build_player_cmd("/a.wav", 1.0) == [
        "ffplay", "-nodisp", "-autoexit", "/a.wav",
    ]


def test_factory_detects_each():
    assert isinstance(make_platform("Darwin"), PlatformMacOS)
    assert isinstance(make_platform("Linux"), PlatformLinux)
    assert isinstance(make_platform("Windows"), PlatformWindows)
