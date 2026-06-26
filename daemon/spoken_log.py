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


# Session → cwd map (daemon process only). Lets append() stamp each entry with
# the project dir the session runs in, so read_merged()/the statusline can scope
# "sub-agent following" to the SAME project: sub-agents and background-agents
# inherit their parent's cwd, while an unrelated concurrent session lives in a
# different dir. Bounded so a long-lived daemon can't accumulate session keys
# without limit. Only the daemon writes entries, so only the daemon needs this;
# the cross-process readers (/tts:log, the statusline wrapper) scope on the
# PERSISTED cwd field instead.
_SESSION_CWD: dict[str, str] = {}
_SESSION_CWD_MAX = 512


def _norm_cwd(cwd: Optional[str]) -> Optional[str]:
    if not isinstance(cwd, str):
        return None
    c = cwd.strip()
    return c or None


def note_session_cwd(session_id: str, cwd: Optional[str]) -> None:
    """Record the cwd a session is running in (called by the daemon on each
    inbound event). Best-effort, bounded, no-op on empty cwd."""
    c = _norm_cwd(cwd)
    if not session_id or not c:
        return
    try:
        if session_id not in _SESSION_CWD and len(_SESSION_CWD) >= _SESSION_CWD_MAX:
            _SESSION_CWD.pop(next(iter(_SESSION_CWD)), None)  # evict oldest
        _SESSION_CWD[session_id] = c
    except Exception:
        pass


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
    cwd = _SESSION_CWD.get(session_id)
    if cwd:
        rec["cwd"] = cwd
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
    cwd: Optional[str] = None,
) -> list[dict]:
    """Merge this session's spoken entries with entries from OTHER session files
    that overlap this session's time span AND ran in the SAME project (cwd).
    Backs the /tts:log "sub-agent aware" view (config
    ``statusline.include_subagent_in_main``). Newest-first, capped at ``limit``.
    Each record gets a ``session`` tag: ``"main"`` for the current session, else
    the source file's short stem.

    cwd scoping (the safe sub-agent-following fix): sub-agents/background-agents
    inherit their parent's cwd, so a sibling file is only folded in when its
    entries carry the SAME ``cwd`` as this session. This is what makes the merge
    safe — without it, an unrelated concurrent top-level session overlapping this
    window would be mixed in (the bug that made two sessions in different dirs
    mirror each other). ``cwd`` may be passed explicitly (e.g. the command's
    ``os.getcwd()``); otherwise it's derived from this session's own most-recent
    entry that recorded one. When no cwd is known (legacy entries predate the
    field), it degrades to the old time-only merge. Single-file view on any error.
    """
    try:
        cur = session_path(session_id)
        cur_stem = _safe_session(session_id)
        cur_entries = _read_all(cur)
        merged = [{**r, "session": "main"} for r in cur_entries]
        ref = now if now is not None else time.time()
        lower = min((r.get("ts", ref) for r in merged), default=ref - default_window_s)
        # Project key: explicit cwd wins; else the newest cwd this session logged.
        cur_cwd = _norm_cwd(cwd) or next(
            (r.get("cwd") for r in reversed(cur_entries) if r.get("cwd")), None
        )
        for f in SPOKEN_DIR.glob("*.jsonl"):
            if f.stem == cur_stem:
                continue
            tag = f.stem[:8]
            for r in _read_all(f):
                if r.get("ts", 0) < lower:
                    continue
                # Same-project gate: when we know our cwd, a sibling entry must
                # match it. Unknown cwd (legacy) → time-only, as before.
                if cur_cwd is not None and r.get("cwd") != cur_cwd:
                    continue
                merged.append({**r, "session": tag})
        merged.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return merged[:limit]
    except Exception:
        return _read_file(session_path(session_id), limit)
