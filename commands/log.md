---
name: tts:log
description: Show a navigable log of recently spoken TTS outputs (newest first), with timestamps and category.
---

# /tts:log — spoken-output log

Print what the TTS daemon has actually spoken, newest first, with timestamps and
category. Reads the per-session JSONL the daemon appends at
`~/.claude/logs/tts/spoken/<session>.jsonl` (written by `daemon/spoken_log.py`).

Optional argument: how many entries to show (default 25). Pass a session id with
`--session <id>` to target a specific session instead of the most-recently-active.

When `statusline.include_subagent_in_main` is enabled in `config.json` and no
`--session` is given, the log shows a MERGED, sub-agent-aware view: lines spoken
by sibling sub-agents / background agents during this session's span are folded
in, each tagged by source (`spoken_log.read_merged()`). Default is off — the
plain single-session view.

Run:

```bash
ARGS="$ARGUMENTS"
N=25; SESSION=""
# parse: optional count and/or --session <id>
set -- $ARGS
while [ $# -gt 0 ]; do
  case "$1" in
    --session) SESSION="$2"; shift 2;;
    ''|*[!0-9]*) shift;;
    *) N="$1"; shift;;
  esac
done
DIR="$HOME/.claude/logs/tts/spoken"
if [ -n "$SESSION" ]; then
  F="$DIR/$(printf '%s' "$SESSION" | tr -c 'A-Za-z0-9_-' '_').jsonl"
else
  F=$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1)
fi
python3 - "$F" "$N" "$SESSION" <<'PY'
# Mirrors daemon/spoken_log.read_merged (the tested reference) — kept inline so
# the command stays self-contained as an installed plugin.
import sys, json, time, os, glob

f = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    n = int(sys.argv[2])
except Exception:
    n = 25
explicit_session = bool(sys.argv[3].strip()) if len(sys.argv) > 3 else False
DIR = os.path.expanduser("~/.claude/logs/tts/spoken")

def read_all(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try: out.append(json.loads(ln))
                    except Exception: pass
    except OSError:
        pass
    return out

def include_subagent_flag():
    cands = []
    if os.environ.get("CLAUDE_TTS_CONFIG"):
        cands.append(os.environ["CLAUDE_TTS_CONFIG"])
    # Canonical config location — matches daemon/paths.py config_path(), honoring
    # XDG_CONFIG_HOME (else ~/.config/claude-tts/config.json). Checked FIRST so the
    # include_subagent_in_main flag is actually read for a standard install.
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    cands.append(os.path.join(xdg, "claude-tts", "config.json"))
    cands.append(os.path.expanduser("~/.claude/tts/config/config.json"))  # legacy fallback
    cands.append(os.path.join(os.getcwd(), "config.json"))
    for c in cands:
        try:
            with open(c, encoding="utf-8") as fh:
                sl = (json.load(fh) or {}).get("statusline", {})
            return bool(sl.get("include_subagent_in_main", False))
        except Exception:
            continue
    return False

if not f:
    print("(no spoken output logged yet — speak something, then try again)")
    raise SystemExit

# include_subagent_in_main: merge sibling-agent lines spoken during this
# (anchor) session's span. Only when no explicit --session was requested.
if include_subagent_flag() and not explicit_session:
    main = [dict(r, session="main") for r in read_all(f)]
    lower = min((r.get("ts", 0) for r in main), default=0)
    recs = list(main)
    anchor = os.path.abspath(f)
    for sib in glob.glob(os.path.join(DIR, "*.jsonl")):
        if os.path.abspath(sib) == anchor:
            continue
        tag = os.path.basename(sib)[:-6][:8]
        for r in read_all(sib):
            if r.get("ts", 0) >= lower:
                recs.append(dict(r, session=tag))
    recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    shown = recs[:n]
    if not shown:
        print("(no spoken output logged yet)"); raise SystemExit
    print(f"# spoken log — MERGED (sub-agent aware)  ({len(shown)} of {len(recs)} entries)")
    for r in shown:
        ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
        cat = r.get("category") or "-"
        src = r.get("session", "-")
        print(f"{ts}  [{src:>8}]  [{cat:>12}]  {r.get('text','')}")
else:
    lines = read_all(f)
    shown = lines[-n:]
    if not shown:
        print("(no spoken output logged yet)"); raise SystemExit
    print(f"# spoken log — {os.path.basename(f)}  ({len(shown)} of {len(lines)} entries)")
    for r in reversed(shown):  # newest first
        ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
        cat = r.get("category") or "-"
        print(f"{ts}  [{cat:>12}]  {r.get('text','')}")
PY
```
