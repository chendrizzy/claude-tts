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
python3 - "$F" "$N" <<'PY'
import sys, json, time
f = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    n = int(sys.argv[2])
except Exception:
    n = 25
if not f:
    print("(no spoken output logged yet — speak something, then try again)")
    raise SystemExit
try:
    lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
except OSError:
    print("(no spoken output logged yet)"); raise SystemExit
shown = [ln for ln in lines if ln.strip()][-n:]
if not shown:
    print("(no spoken output logged yet)"); raise SystemExit
import os
print(f"# spoken log — {os.path.basename(f)}  ({len(shown)} of {len(lines)} entries)")
for ln in reversed(shown):  # newest first
    try:
        r = json.loads(ln)
    except Exception:
        continue
    ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0)))
    cat = r.get("category") or "-"
    print(f"{ts}  [{cat:>12}]  {r.get('text','')}")
PY
```
