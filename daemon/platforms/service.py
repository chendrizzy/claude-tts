"""Pure launchd service-file rendering for the claude-tts daemon.

Kept separate from base.py (audio playback) because service templating is a
distinct responsibility. Pure: builds a dict and serializes via stdlib plistlib,
so the gate test can round-trip it. The per-machine absolute paths in the OUTPUT
are caller-supplied and correct for launchd; the renderer itself hardcodes no
personal data. Linux systemd rendering is Plan 4.
"""
from __future__ import annotations

import plistlib
from typing import Dict, List

DEFAULT_LABEL = "com.claude-tts.daemon"  # sanitized; matches the fork's hooks.


def render_launchd_plist(
    *,
    program_args: List[str],
    env: Dict[str, str],
    stdout_path: str,
    stderr_path: str,
    label: str = DEFAULT_LABEL,
    throttle_interval: int = 10,
) -> str:
    """Return a launchd plist XML string (RunAtLoad + KeepAlive user agent)."""
    plist: Dict[str, object] = {
        "Label": label,
        "ProgramArguments": list(program_args),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": throttle_interval,
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
    }
    return plistlib.dumps(plist).decode("utf-8")
