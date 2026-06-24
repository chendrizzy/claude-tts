#!/bin/bash
# TTS Session Start Hook
# Ensures TTS daemon is running when Claude Code session starts
# This is LAYER 2 of the multi-layer daemon management system

# ============================================================================
# LAYER 2: SESSION START HOOK (for marketplace-only installations)
# ============================================================================
#
# This hook fires when Claude Code starts a session and ensures the TTS
# daemon is running. It's a fallback for users who don't have a custom
# launcher with the -yts flag integration.
#
# Hook execution: SessionStart (startup|resume)
# Priority: High (must complete before other hooks)
# ============================================================================

# Determine plugin root
PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_DIR="$PLUGIN_ROOT/daemon"
DAEMON_SCRIPT="$DAEMON_DIR/tts_daemon.py"
SOCKET="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
LOG_DIR="$PLUGIN_ROOT/logs"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi
LAUNCHD_LABEL="com.claude-tts.daemon"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
STARTUP_TIMEOUT_SECONDS="${CLAUDE_TTS_STARTUP_TIMEOUT_SECONDS:-15}"
STARTUP_ATTEMPTS=$((STARTUP_TIMEOUT_SECONDS * 10))

# Create log directory
mkdir -p "$LOG_DIR" 2>/dev/null

# Log to stderr (visible in Claude Code logs)
log() {
    echo "[TTS SessionStart] $1" >&2
}

# ============================================================================
# Daemon Status Check (Multi-Level)
# ============================================================================

# Level 1: Process check (most reliable)
if /usr/bin/pgrep -f "tts_daemon.py" > /dev/null 2>&1; then
    log "✅ Daemon already running (process check)"
    exit 0
fi

# Level 2: stale socket cleanup. A live daemon would have been caught above.
if [ -S "$SOCKET" ]; then
    log "⚠️  Stale socket detected - cleaning up"
    rm -f "$SOCKET" 2>/dev/null
fi

# ============================================================================
# Start Daemon
# ============================================================================

log "🚀 Starting TTS daemon..."

# Check if daemon script exists
if [ ! -f "$DAEMON_SCRIPT" ]; then
    log "❌ Daemon script not found: $DAEMON_SCRIPT"
    exit 1
fi

# Check Python availability
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    log "❌ Python 3 not found - cannot start daemon"
    exit 1
fi

# Prefer launchd so the daemon is owned by the user session, not by Claude's
# short-lived hook process tree.
if [ -f "$LAUNCHD_PLIST" ] && command -v launchctl >/dev/null 2>&1; then
    UID_VALUE="$(id -u)"
    launchctl bootstrap "gui/${UID_VALUE}" "$LAUNCHD_PLIST" 2>/dev/null || true
    launchctl enable "gui/${UID_VALUE}/${LAUNCHD_LABEL}" 2>/dev/null || true
    launchctl kickstart -k "gui/${UID_VALUE}/${LAUNCHD_LABEL}" 2>/dev/null || true
    DAEMON_PID="launchd:${LAUNCHD_LABEL}"
else
    # Fallback for non-launchd environments.
    nohup "$PYTHON_BIN" "$DAEMON_SCRIPT" >> "$LOG_DIR/tts_daemon.log" 2>&1 </dev/null &
    DAEMON_PID=$!
fi

# Wait for daemon to be ready. Python 3.14 cold starts plus dependency imports
# can exceed 3s on this machine; killing early creates repeated hook retries.
i=1
while [ "$i" -le "$STARTUP_ATTEMPTS" ]; do
    if [ -S "$SOCKET" ]; then
        log "✅ Daemon started successfully ($DAEMON_PID)"
        exit 0
    fi
    sleep 0.1
    i=$((i + 1))
done

# ============================================================================
# Startup Failed
# ============================================================================

log "❌ Daemon failed to start within ${STARTUP_TIMEOUT_SECONDS} seconds"
log "Check logs: $LOG_DIR/tts_daemon.log"

# Try to kill the daemon if it's stuck
if [ "${DAEMON_PID#launchd:}" = "$DAEMON_PID" ]; then
    kill "$DAEMON_PID" 2>/dev/null || true
fi

exit 1
