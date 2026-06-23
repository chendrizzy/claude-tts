#!/bin/bash
# Restart TTS Daemon — hardened version (Wave 1 W1.A)
#
# Hardening over the original script:
#   * Polls for socket binding by the new PID after start (catches the
#     "old daemon survived pkill, new daemon couldn't bind" failure mode).
#   * Aborts loudly with non-zero exit if the new daemon never owns the
#     socket within the deadline.
#   * Singleton-check on the daemon side (added in tts_daemon.py.run())
#     means a failed pkill will now refuse the second start cleanly.

set -u

SOCKET_PATH="/tmp/tts_daemon.sock"
PID_FILE="$HOME/.claude/tts_daemon.pid"
DEADLINE_SECONDS=15

echo "========================================================================="
echo "TTS DAEMON RESTART"
echo "========================================================================="
echo ""

# Preferred path: the daemon is normally a launchd job (see hooks/session-start.sh).
# pkill would FIGHT launchd (it just respawns) and historically matched the wrong
# process name. launchctl kickstart -k kills + relaunches with on-disk code.
LAUNCHD_LABEL="com.claude-tts.daemon"
UID_VALUE="$(id -u)"
if command -v launchctl >/dev/null 2>&1 && \
   launchctl print "gui/${UID_VALUE}/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
    echo "Daemon is launchd-managed — restarting via launchctl kickstart..."
    launchctl kickstart -k "gui/${UID_VALUE}/${LAUNCHD_LABEL}"
    deadline=$((SECONDS + DEADLINE_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if [ -S "$SOCKET_PATH" ]; then
            echo "Daemon restarted (launchd: ${LAUNCHD_LABEL}); socket bound."
            exit 0
        fi
        sleep 0.5
    done
    echo "ERROR: launchd daemon did not bind $SOCKET_PATH within ${DEADLINE_SECONDS}s."
    exit 1
fi

# Fallback (non-launchd environments): stop the running daemon by name.
DAEMON_PID=$(pgrep -f "tts_daemon.py" || true)

if [ -n "${DAEMON_PID:-}" ]; then
    echo "Stopping current daemon (PIDs: $DAEMON_PID)..."
    # shellcheck disable=SC2086
    kill $DAEMON_PID 2>/dev/null || true
    sleep 1

    # Check if anything survived
    if pgrep -f "tts_daemon.py" > /dev/null; then
        echo "Daemon didn't stop gracefully, force killing..."
        pkill -9 -f "tts_daemon.py" 2>/dev/null || true
        sleep 1
    fi

    # Final survivor check — if anything is still around, abort BEFORE
    # starting a new daemon. The singleton check in run() would catch it
    # too, but failing fast here is friendlier.
    if pgrep -f "tts_daemon.py" > /dev/null; then
        echo "ERROR: failed to kill old daemon. Aborting restart."
        echo "Surviving PIDs: $(pgrep -f 'tts_daemon.py')"
        exit 1
    fi
    echo "Old daemon stopped"
else
    echo "No daemon was running"
fi

# Stale PID/socket cleanup so the new daemon starts in a clean state.
[ -e "$SOCKET_PATH" ] && rm -f "$SOCKET_PATH"
[ -e "$PID_FILE" ] && rm -f "$PID_FILE"

echo ""
echo "Starting daemon..."
cd "$(dirname "$0")"
export CLAUDE_TTS_RATE="+6%"
export CLAUDE_TTS_PITCH="+3Hz"
python3 daemon/tts_daemon.py &
NEW_PID=$!

# Post-start verification: wait for THIS PID to own the socket. lsof on
# the socket file must show the new pid before the deadline expires.
echo "Verifying new daemon (PID $NEW_PID) bound the socket..."
deadline=$((SECONDS + DEADLINE_SECONDS))
bound=0
while [ "$SECONDS" -lt "$deadline" ]; do
    # First make sure the process is still alive
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "ERROR: new daemon (PID $NEW_PID) exited before binding socket."
        echo "Tail of recent log:"
        tail -20 ~/.claude/logs/tts/tts_daemon.log 2>/dev/null || true
        exit 1
    fi
    if [ -S "$SOCKET_PATH" ] && lsof "$SOCKET_PATH" 2>/dev/null | grep -q " $NEW_PID "; then
        bound=1
        break
    fi
    sleep 0.5
done

if [ "$bound" -ne 1 ]; then
    echo "ERROR: PID $NEW_PID did not bind $SOCKET_PATH within ${DEADLINE_SECONDS}s."
    echo "lsof output:"
    lsof "$SOCKET_PATH" 2>/dev/null || echo "  (socket file does not exist)"
    echo "Process status:"
    ps -p "$NEW_PID" -o pid,ppid,state,command 2>/dev/null || echo "  (PID $NEW_PID not found)"
    # Try to kill the half-started daemon so we leave the system clean
    kill "$NEW_PID" 2>/dev/null || true
    exit 1
fi

echo "Daemon bound socket successfully (PID: $NEW_PID)"
echo ""
echo "========================================================================="
echo "TTS DAEMON RUNNING"
echo "========================================================================="
echo "  PID:    $NEW_PID"
echo "  Socket: $SOCKET_PATH"
echo "  Log:    ~/.claude/logs/tts/tts_daemon.log"
echo "========================================================================="
