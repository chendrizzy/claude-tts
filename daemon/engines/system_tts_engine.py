"""Zero-dependency fallback TTS engine.

Shells out to the OS-native speech synthesizer — macOS ``say``, Linux
``espeak``/``espeak-ng`` — so TTS works on a bare machine with no Python ML
dependencies and no network. Like the rest of the engine seam, ``synthesize``
*writes* an audio file to ``out_path`` and returns success; playback stays in
``PlaybackStage``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
from typing import List, Optional

from daemon.engines.base import TTSEngine

logger = logging.getLogger(__name__)

# say/espeak default cadence (words per minute); ``speed`` scales this.
_DEFAULT_WPM = 175


class SystemTTSEngine(TTSEngine):
    """TTSEngine backed by the OS speech binary (say/espeak). No deps, no network."""

    def _build_cmd(
        self, text: str, out_path: str, speed: float
    ) -> Optional[List[str]]:
        """Build the argv for the OS speech binary, or None if none is available.

        Forces a WAVE container so the written file is a valid WAV.  The pipeline
        names the cache file ``.wav`` (via ``_audio_ext_for``) so that
        extension-keyed players (macOS afplay dispatches by extension, not content)
        open it correctly.  ``text`` is placed after a ``--`` end-of-options
        separator — passed via exec (no shell), so it is injection-safe and a
        leading-dash chunk is treated as text, not an option flag.
        """
        wpm = max(80, int(_DEFAULT_WPM * speed)) if speed and speed > 0 else _DEFAULT_WPM
        if platform.system() == "Darwin":
            cmd = ["say", "--file-format=WAVE", "--data-format=LEI16@22050",
                   "-o", out_path]
            if speed and speed != 1.0:
                cmd += ["-r", str(wpm)]
            cmd += ["--", text]
            return cmd
        # Linux / other: espeak (or espeak-ng) writes WAV with -w.
        binary = shutil.which("espeak") or shutil.which("espeak-ng")
        if not binary:
            return None
        cmd = [binary, "-w", out_path]
        if speed and speed != 1.0:
            cmd += ["-s", str(wpm)]
        cmd += ["--", text]
        return cmd

    async def synthesize(
        self, text: str, out_path: str, voice: str, speed: float = 1.0
    ) -> bool:
        # ponytail: voice is ignored — OS default voice. Per-OS voice maps are
        # YAGNI; add when a user asks for a specific system voice.
        cmd = self._build_cmd(text, out_path, speed)
        if cmd is None:
            logger.error("no system TTS binary (say/espeak) available")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await proc.wait()
        except Exception as exc:  # noqa: BLE001 — match EdgeTTSEngine: never raise into the pipeline
            logger.error("system TTS synth failed: %s", exc)
            return False
        return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
