"""Platform — spawn the OS audio player for a synthesized file, and (macOS)
install/uninstall the background service.

Only the player subprocess differs per OS; the surrounding playback bookkeeping
(state, await, FD-cleanup) lives in PlaybackStage and is platform-agnostic.
build_player_cmd() is pure (unit-tested). macOS service install is implemented
here (launchd); Linux systemd install is Plan 4.
"""
from __future__ import annotations

import asyncio
import os
import platform as _platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from daemon.platforms.service import render_launchd_plist, DEFAULT_LABEL


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

    def install_service(self, *, program_args: List[str], env: dict) -> None:
        """Install + start the background service. Overridden per-OS."""
        raise NotImplementedError("service install not implemented for this platform")

    def uninstall_service(self) -> None:
        """Stop + remove the background service. Overridden per-OS."""
        raise NotImplementedError("service uninstall not implemented for this platform")


class PlatformMacOS(Platform):
    LABEL = DEFAULT_LABEL

    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        cmd = ["afplay"]
        if volume and volume != 1.0:
            cmd += ["-v", f"{volume:.3f}"]
        cmd.append(audio_path)
        return cmd

    def plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.LABEL}.plist"

    def render_service(self, *, program_args: List[str], env: dict) -> str:
        return render_launchd_plist(
            program_args=program_args,
            env=env,
            stdout_path="/tmp/claude-tts-daemon.out.log",
            stderr_path="/tmp/claude-tts-daemon.err.log",
            label=self.LABEL,
        )

    def install_service(self, *, program_args: List[str], env: dict) -> None:
        path = self.plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_service(program_args=program_args, env=env),
                        encoding="utf-8")
        uid = os.getuid()
        # Re-bootstrap idempotently: bootout any stale instance, then bootstrap.
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{self.LABEL}"],
                       capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                       check=True, capture_output=True)
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{self.LABEL}"],
                       capture_output=True)

    def uninstall_service(self) -> None:
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{self.LABEL}"],
                       capture_output=True)
        self.plist_path().unlink(missing_ok=True)


# Linux audio players, decoders-first. ponytail: ffplay/mpv decode BOTH .wav
# (espeak) and .mp3 (edge-tts); pw-play/paplay/aplay are WAV-only and would fail
# silently on an .mp3, so they are the tail — reached only on minimal boxes,
# where the engine is espeak → WAV anyway. Upgrade path: if mp3-on-minimal
# becomes common, document "edge-tts on Linux needs ffplay or mpv".
_LINUX_PLAYERS = ("ffplay", "mpv", "pw-play", "paplay", "aplay")


def _linux_player_argv(player: str, audio_path: str) -> List[str]:
    """Quiet, no-GUI argv for a given Linux player."""
    if player == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", audio_path]
    if player == "mpv":
        return ["mpv", "--no-video", "--really-quiet", audio_path]
    if player == "aplay":
        return ["aplay", "-q", audio_path]
    # pw-play / paplay: the filename is the only required positional arg.
    return [player, audio_path]


class PlatformLinux(Platform):
    def build_player_cmd(self, audio_path: str, volume: float) -> List[str]:
        # volume intentionally ignored: TTS-specific gain is macOS-only (afplay -v);
        # on Linux the audio daemon (PipeWire/PulseAudio/ALSA) owns system volume.
        for player in _LINUX_PLAYERS:
            if shutil.which(player):
                return _linux_player_argv(player, audio_path)
        # Nothing installed: best-effort mpv argv (fails loudly via rc != 0).
        return _linux_player_argv("mpv", audio_path)


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
