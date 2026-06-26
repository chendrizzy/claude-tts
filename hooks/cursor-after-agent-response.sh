#!/bin/bash
# Cursor afterAgentResponse -> daemon stop_event (assistant final answer)

set -u

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="/tmp/tts_daemon.sock"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

INPUT=$(cat)

TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
if [[ "$TTS_ENABLED" != "true" ]]; then
    exit 0
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    exit 0
fi

LOG_DIR="$HOME/.claude/logs/hooks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cursor_after_agent_$(date +%Y%m%d).log"
log() { echo "[$(date +'%H:%M:%S')] Cursor-AfterAgent: $1" >> "$LOG_FILE"; }

if [ ! -S "$SOCKET" ] || ! /usr/bin/pgrep -f "python.*tts_daemon.py" > /dev/null 2>&1; then
    source "$PLUGIN_ROOT/hooks/ensure-daemon-ready.sh" 2>>"$LOG_FILE" || true
    if [ ! -S "$SOCKET" ]; then
        log "WARN: daemon unavailable — skipping stop_event"
        exit 0
    fi
fi

NORMALIZED=$("$PYTHON_BIN" "$PLUGIN_ROOT/hooks/cursor_normalize.py" agent_response <<< "$INPUT" 2>/dev/null) || exit 0
[ -n "$NORMALIZED" ] || exit 0

(
    printf '%s' "$NORMALIZED" | timeout 8 "$PYTHON_BIN" -c "
import json, socket, sys, time, uuid

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)

content = (data.get('content') or '').strip()
if not content:
    sys.exit(0)

payload = {
    'command': 'stop_event',
    'session_id': data.get('session_id') or 'default',
    'content': content,
    'transcript_path': data.get('transcript_path') or '',
    'stop_hook_active': bool(data.get('stop_hook_active', False)),
    'event_id': str(uuid.uuid4()),
    'ts': time.time(),
}

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect('$SOCKET')
    s.sendall((json.dumps(payload) + '\n').encode())
    s.recv(4096)
finally:
    s.close()
" 2>>"$LOG_FILE"
) &

log "stop_event dispatched"
exit 0
