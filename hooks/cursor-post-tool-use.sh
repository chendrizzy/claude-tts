#!/bin/bash
# Cursor postToolUse -> Claude Code-shaped tool_event (via post-tool-use.sh)

set -u

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

NORMALIZED=$("$PYTHON_BIN" "$PLUGIN_ROOT/hooks/cursor_normalize.py" post <<< "$INPUT" 2>/dev/null) || exit 0
[ -n "$NORMALIZED" ] || exit 0

# Cursor postToolUse must not echo stdin back (unlike Claude Code hook chains).
CLAUDE_TTS_PASSTHROUGH=false bash "$PLUGIN_ROOT/hooks/post-tool-use.sh" <<< "$NORMALIZED" >/dev/null 2>&1 &
exit 0
