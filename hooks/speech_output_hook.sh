#!/bin/bash
# Speech Output Hook for Claude Code TTS — Stop-hook handler
#
# Wave 3 W3.A cutover: legacy `client.speak()` Python block REMOVED. ALL
# events now route through the new `stop_event` socket endpoint, which
# feeds ContentRouter → QueueManager → pipeline. The Wave 2.5 shadow
# dual-write is now the PRIMARY path (no `shadow: true` flag).
#
# Wired by ~/.claude/settings.json to:
#   - Stop  (matcher: "*")     → JSON Stop payload with transcript_path
#   - Notification (matcher: "")  → may be raw text (legacy leftover, harmless)
#
# Stop_hook_active is honored by the daemon (skipped to prevent infinite
# loops). Transcript-path JSONL extraction handled inline below.

# Read the output from stdin
OUTPUT=$(cat)

# Always echo the output first (passthrough behavior)
echo "$OUTPUT"

# Skip if TTS is disabled
TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
if [[ "$TTS_ENABLED" != "true" ]]; then
    exit 0
fi

# Logging
LOG_DIR="$HOME/.claude/logs/hooks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/speech_output_$(date +%Y%m%d).log"
log() { echo "[$(date +'%H:%M:%S')] Speech-Output: $1" >> "$LOG_FILE"; }
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# Require python3 — degrade gracefully on missing deps.
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    log "WARN: python3 unavailable — skipping daemon submission"
    exit 0
fi

# Defensive readiness check. Degrade gracefully — never `exit 0` on failure
# in a way that hides the daemon being down; just skip the submission.
SOCKET="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
if [ ! -S "$SOCKET" ]; then
    log "WARN: daemon socket missing — skipping stop_event submission"
    exit 0
fi

# Build + dispatch stop_event in background. Reads transcript_path from JSON
# Stop payload, walks the JSONL backwards to find the latest assistant text,
# then forwards as `stop_event`. If input is raw text (Notification path),
# treat it as content directly.
(
    printf '%s' "$OUTPUT" | timeout 8 "$PYTHON_BIN" -c "
import sys, json, socket, time, uuid, os
from pathlib import Path

raw = sys.stdin.read()

# Try to parse as JSON (Stop hook payload). If not JSON, treat raw as content.
try:
    payload = json.loads(raw)
    session_id = payload.get('session_id', 'default')
    transcript_path = payload.get('transcript_path', '')
    stop_hook_active = bool(payload.get('stop_hook_active', False))
except Exception:
    payload = {}
    session_id = os.environ.get('CLAUDE_SESSION_ID', 'default')
    transcript_path = ''
    stop_hook_active = False

# Extract latest assistant message from transcript JSONL.
content = ''
if transcript_path and Path(transcript_path).exists():
    try:
        # Tail the file — the latest assistant entry is near the end.
        with open(transcript_path, 'r') as f:
            lines = f.readlines()
        # Walk backwards to find the most recent role=assistant text.
        for line in reversed(lines[-50:]):  # last 50 lines is plenty
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Multiple Claude Code transcript shapes — try each.
            if rec.get('role') == 'assistant' or rec.get('type') == 'assistant':
                msg = rec.get('message') or rec.get('content') or ''
                if isinstance(msg, dict):
                    parts = msg.get('content', [])
                    if isinstance(parts, list):
                        for p in parts:
                            if isinstance(p, dict) and p.get('type') == 'text':
                                content = p.get('text', '')
                                break
                            elif isinstance(p, str):
                                content = p; break
                    elif isinstance(parts, str):
                        content = parts
                elif isinstance(msg, str):
                    content = msg
                if content: break
    except Exception as e:
        sys.stderr.write(f'transcript-read failed: {e}\n')
elif raw and not raw.startswith('{'):
    # Notification path: raw stdin IS the content.
    content = raw

if not content:
    # Nothing useful to classify; bail silently.
    sys.exit(0)

# Build stop_event payload (PRIMARY path — no shadow flag).
payload_out = {
    'command': 'stop_event',
    'session_id': session_id,
    'content': content,
    'transcript_path': transcript_path,
    'stop_hook_active': stop_hook_active,
    'event_id': str(uuid.uuid4()),
    'ts': time.time(),
}

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect('$SOCKET')
    s.sendall((json.dumps(payload_out) + '\n').encode())
    s.recv(4096)  # drain response
finally:
    s.close()
" 2>>"$LOG_FILE"
) &

log "stop_event dispatched"
exit 0
