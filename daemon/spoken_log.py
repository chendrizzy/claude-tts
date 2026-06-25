"""Per-session log of spoken TTS utterances — backs the statusline segment and
the /tts:log command.

Append-only JSONL at ``~/.claude/logs/tts/spoken/<session_id>.jsonl``. Design
constraints:

- **Best-effort, never raises into the playback path.** A logging failure must
  not silence TTS, so every public function swallows its own errors and returns
  a falsy/empty value instead of propagating.
- **Bounded.** Each session file is trimmed to the most recent ``MAX_LINES`` so
  it cannot grow without limit (the daemon is long-lived).
- **Keyed by session_id** (the only stable handle the daemon has per event). A
  Claude Code session is scoped to one project/cwd, so "this session's spoken
  log" is effectively "this project's spoken output". The statusline and the
  /tts:log command resolve the current session from their own context.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

SPOKEN_DIR = Path.home() / ".claude" / "logs" / "tts" / "spoken"
MAX_LINES = 500  # most recent N spoken utterances retained per session


def _safe_session(session_id: str) -> str:
    """Filesystem-safe file stem (sessions are UUIDs; be defensive anyway)."""
    sid = session_id or "default"
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in sid)[:128]


def session_path(session_id: str) -> Path:
    return SPOKEN_DIR / f"{_safe_session(session_id)}.jsonl"


def append(
    text: str,
    *,
    session_id: str,
    category: Optional[str] = None,
    ts: Optional[float] = None,
) -> bool:
    """Append one spoken utterance. Best-effort: returns False on any failure
    rather than raising, so the playback path is never broken by logging."""
    if not text or not text.strip():
        return False
    rec = {
        "ts": ts if ts is not None else time.time(),
        "text": text.strip(),
        "category": category,
    }
    try:
        SPOKEN_DIR.mkdir(parents=True, exist_ok=True)
        p = session_path(session_id)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim(p)
        return True
    except Exception:
        return False


def _trim(p: Path) -> None:
    """Cap a session file at MAX_LINES (keep the newest). Best-effort."""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > MAX_LINES:
            p.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def read_recent(session_id: str, limit: int = 20) -> list[dict]:
    """Most recent ``limit`` utterances, NEWEST FIRST. Empty list on any error."""
    return _read_file(session_path(session_id), limit)


def _read_file(p: Path, limit: int) -> list[dict]:
    try:
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        out: list[dict] = []
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def latest_session_file() -> Optional[Path]:
    """Most-recently-written session log — the default target for the statusline
    segment and /tts:log when no explicit session is given. None if none exist."""
    try:
        files = list(SPOKEN_DIR.glob("*.jsonl"))
        if not files:
            return None
        return max(files, key=lambda f: f.stat().st_mtime)
    except Exception:
        return None


def read_latest(limit: int = 20) -> list[dict]:
    """Most recent utterances from the most-recently-active session, newest first."""
    p = latest_session_file()
    return _read_file(p, limit) if p is not None else []
