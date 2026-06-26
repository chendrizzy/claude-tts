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


def _read_all(p: Path) -> list[dict]:
    """All records in a session file, in FILE ORDER (oldest first). [] on error."""
    try:
        if not p.exists():
            return []
        out: list[dict] = []
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _read_file(p: Path, limit: int) -> list[dict]:
    return list(reversed(_read_all(p)))[:limit]


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


def read_merged(
    session_id: str,
    *,
    limit: int = 25,
    now: Optional[float] = None,
    default_window_s: float = 14_400.0,
) -> list[dict]:
    """Merge this session's spoken entries with entries from OTHER session files
    that overlap this session's time span. Backs the /tts:log "sub-agent aware"
    view (config ``statusline.include_subagent_in_main``). Newest-first, capped
    at ``limit``. Each record gets a ``session`` tag: ``"main"`` for the current
    session, else the source file's short stem.

    "belonging" is inferred by time overlap, not a real parent link — Claude Code
    gives sub-agents/background-agents independent session_ids with NO parent
    pointer at the hook layer, so a concurrent unrelated top-level session that
    overlaps this one's window can be folded in. Acceptable for a read-only view.
    Degrades to the single-file view on any error.
    """
    try:
        cur = session_path(session_id)
        cur_stem = _safe_session(session_id)
        merged = [{**r, "session": "main"} for r in _read_all(cur)]
        ref = now if now is not None else time.time()
        lower = min((r.get("ts", ref) for r in merged), default=ref - default_window_s)
        for f in SPOKEN_DIR.glob("*.jsonl"):
            if f.stem == cur_stem:
                continue
            tag = f.stem[:8]
            for r in _read_all(f):
                if r.get("ts", 0) >= lower:
                    merged.append({**r, "session": tag})
        merged.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return merged[:limit]
    except Exception:
        return _read_file(session_path(session_id), limit)
