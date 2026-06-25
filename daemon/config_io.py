"""Assemble + persist the portable claude-tts user config.

render_config is pure (caller-supplied values only — no personal data, no
absolute paths). write_config serializes to config_path() (or an explicit path)
with owner-only perms, since the config may hold an API key. The schema matches
what daemon/tts_daemon._load_tts_user_config reads (provider selector is
`llm_provider.type`; voice is `voice.engine` + `voice.name`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from daemon.paths import config_path


def render_config(*, engine: str, voice_name: str, backend: str,
                  model: str = "", base_url: str = "", api_key: str = "") -> dict:
    """Build the config dict for the chosen engine + LLM backend."""
    cfg: dict = {
        "voice": {"engine": engine, "name": voice_name, "rate": 1.0, "volume": 1.0},
    }
    backend = backend.lower()
    if backend == "null":
        cfg["llm_provider"] = {"type": "null"}
    elif backend == "openai":
        cfg["llm_provider"] = {
            "type": "openai", "base_url": base_url, "model": model, "api_key": api_key,
        }
    else:  # ollama
        cfg["llm_provider"] = {"type": "ollama"}
        cfg["summarizer"] = {"model": model, "inner_timeout_s": 3.5,
                             "keep_alive": "30m", "warm_interval_s": 120.0}
    return cfg


def write_config(cfg: dict, path: Optional[Path] = None) -> Path:
    """Serialize cfg to `path` (default config_path()) with 0700 dir / 0600 file."""
    dest = Path(path) if path is not None else config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    dest.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(dest, 0o600)
    return dest
