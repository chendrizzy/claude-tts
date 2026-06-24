"""Platform — spawn the OS audio player for a synthesized file.

Only the player subprocess differs per OS; the surrounding playback bookkeeping
(state, await, FD-cleanup) lives in PlaybackStage and is platform-agnostic.
build_player_cmd() is pure (unit-tested); spawn_player() runs it. Service
install/uninstall (launchd/systemd) is intentionally NOT here — it lands with
the plugin setup (Plan 3).
"""
from __future__ import annotations

import asyncio
import platform as _platform
from abc import ABC, abstractmethod
from typing import List, Optional


class Platform(ABC):
    @abstractmethod
    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        """The exact argv for the OS audio player. Pure — no side effects."""
        raise NotImplementedError

    async def spawn_player(self, audio_path: str, volume: float):
        """Spawn the player subprocess (stdout/stderr to DEVNULL)."""
        cmd = self.build_player_cmd(audio_path, volume)
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )


class PlatformMacOS(Platform):
    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        cmd = ["afplay"]
        if volume and volume != 1.0:
            cmd += ["-v", f"{volume:.3f}"]
        cmd.append(audio_path)
        return cmd


class PlatformLinux(Platform):
    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        # Matches prior inline behavior: mpv, volume not applied.
        return ["mpv", "--no-video", "--really-quiet", audio_path]


class PlatformWindows(Platform):
    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        return ["ffplay", "-nodisp", "-autoexit", audio_path]


def make_platform(system: Optional[str] = None) -> Platform:
    name = (system or _platform.system()).lower()
    if name == "darwin":
        return PlatformMacOS()
    if name == "linux":
        return PlatformLinux()
    return PlatformWindows()
