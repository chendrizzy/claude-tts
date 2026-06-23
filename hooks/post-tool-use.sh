#!/bin/bash
# Post-Tool-Use Hook for Claude Code TTS
#
# Wave 3 W3.A cutover: legacy `enhanced_hook_integration.process_hook_input`
# block REMOVED. ALL events now route through the new `tool_event` socket
# endpoint, which feeds ContentRouter → QueueManager → pipeline. The Wave 2.5
# shadow dual-write is now the PRIMARY path (no `shadow: true` flag).

# Read JSON input from stdin
INPUT=$(cat)

# Always echo the input first (passthrough so subsequent hooks see data)
echo "$INPUT"

# Skip if TTS is disabled
TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
if [[ "$TTS_ENABLED" != "true" ]]; then
    exit 0
fi

# Logging
LOG_DIR="$HOME/.claude/logs/hooks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/post_tool_$(date +%Y%m%d).log"
log() { echo "[$(date +'%H:%M:%S')] Post-Tool: $1" >> "$LOG_FILE"; }

# Plugin root (one level up from hooks/) — kept for ensure-daemon-ready.sh.
PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# Require python3 + jq for payload assembly. Degrade gracefully — never `exit 0`
# on failure that would prevent later hooks from firing; just skip the daemon
# call and log it.
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    log "WARN: python3 unavailable — skipping daemon submission"
    exit 0
fi
if ! command -v jq &>/dev/null; then
    log "WARN: jq unavailable — skipping daemon submission"
    exit 0
fi

# Defensive readiness check. If the socket is missing or unresponsive, attempt
# to spin up the daemon via the helper. Degrade gracefully — log and skip the
# submission rather than failing the hook (we already echoed INPUT above).
SOCKET="/tmp/tts_daemon.sock"
if [ ! -S "$SOCKET" ] || ! /usr/bin/pgrep -f "python.*tts_daemon.py" > /dev/null 2>&1; then
    log "Daemon not ready — attempting ensure-daemon-ready"
    source "$PLUGIN_ROOT/hooks/ensure-daemon-ready.sh" 2>&1 | tee -a "$LOG_FILE" >&2
    if [ ! -S "$SOCKET" ]; then
        log "WARN: daemon still unavailable — skipping tool_event submission"
        exit 0
    fi
fi

# Forward the raw Claude Code payload to the daemon as a `tool_event` with
# phase=post. The daemon's ContentRouter classifies + routes; the wrapping
# fields (command/phase/event_id/ts) are added on top of the existing payload.
# Runs in background so the hook returns immediately.
(
    EVENT_ID=$("$PYTHON_BIN" -c "import uuid; print(uuid.uuid4())" 2>/dev/null || echo "")
    TS=$("$PYTHON_BIN" -c "import time; print(time.time())" 2>/dev/null || date +%s)
    PAYLOAD=$(jq -c \
        --arg cmd "tool_event" \
        --arg phase "post" \
        --arg eid "$EVENT_ID" \
        --argjson ts "$TS" \
        '. + {command: $cmd, phase: $phase, event_id: $eid, ts: $ts}' \
        <<< "$INPUT" 2>/dev/null)
    if [ -n "$PAYLOAD" ]; then
        timeout 5 "$PYTHON_BIN" -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect('$SOCKET')
    s.sendall(sys.argv[1].encode() + b'\n')
    s.recv(4096)  # drain response, don't block on it
finally:
    s.close()
" "$PAYLOAD" 2>>"$LOG_FILE"
    else
        log "ERROR: failed to assemble tool_event payload"
    fi
) &

log "tool_event dispatched"
exit 0
