#!/bin/bash
# Pre-Tool-Use Hook — long-running Bash heuristic + AskUserQuestion only
#
# Wave 1 rewrite (W1.A): Drop the ToolTenseManager mechanical narration
# ("Reading X" / "Read X") that was the primary noise source. Per the
# overhaul plan, this hook ONLY announces for Bash invocations whose
# command line matches a long-running heuristic regex. Everything else
# exits 0 silently so subsequent hooks see no extra activity.
#
# Wave 3 W3.A decision: KEEP these two pre-announcements on the legacy
# `speak` socket endpoint. ContentRouter explicitly silences phase=pre
# events ("pre-tool phase: silence by policy"), so routing them through
# `tool_event` would mute them. They're short, time-sensitive (must speak
# BEFORE the blocking user-question or long-running cargo/test invocation),
# and don't benefit from classification/summarization. The legacy `speak`
# endpoint stays in tts_daemon.py specifically for these two cases.
#
# Phase 4 HOOK-03 / ANNOUNCE-02 rewrite:
#   - Uses \btest\b word-boundary regex (not 'test' substring)
#   - Runner allowlist: pytest, cargo test, npm test, jest, vitest, mocha,
#     make test, go test, cargo build/run/check/clippy
#   - Deny-list: shell operators (&&, ||, ;, |, >, <, 2>&1, 2>, &) must
#     not appear as the announcement target
#   - Strips after first unquoted shell operator before pattern matching
#   - ANNOUNCE-02: if no allowlist match, exit silently (no generic fallback)
#   - More substantive announcements: runner + target file basename or dir

INPUT=$(cat)
# Cursor wrappers set CLAUDE_TTS_PASSTHROUGH=false so postToolUse stdout stays clean.
if [[ "${CLAUDE_TTS_PASSTHROUGH:-true}" == "true" ]]; then
    echo "$INPUT"
fi

TTS_ENABLED="${CLAUDE_TTS_ENABLED:-true}"
[[ "$TTS_ENABLED" != "true" ]] && exit 0

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

# Session id MUST come from the JSON payload — Claude Code does NOT set
# CLAUDE_SESSION_ID as an env var.
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "default"' 2>/dev/null)
SOCKET_PATH="${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}"
PYTHON_BIN="${CLAUDE_TTS_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi

# AskUserQuestion: announce the FIRST question text BEFORE the user answers.
# PreToolUse fires when Claude initiates the tool call; PostToolUse only
# fires after the user has already answered, by which point the audio is
# pointless. Speak with HIGH priority so it jumps any pending status chatter.
if [[ "$TOOL_NAME" == "AskUserQuestion" ]]; then
    QUESTION=$(echo "$INPUT" | jq -r '.tool_input.questions[0].question // empty' 2>/dev/null)
    if [[ -n "$QUESTION" && -S "$SOCKET_PATH" && -x "$PYTHON_BIN" ]]; then
        # ANNOUNCE-01: cap at first sentence OR ≤80 chars, whichever is shorter.
        # A 50-word question produces 15-25 s of speech — far too long for a
        # pre-answer cue. We want a brief "attention ping", not a reading.
        QUESTION=$("$PYTHON_BIN" -c "
import re, sys
q = sys.argv[1]
# Split on sentence-ending punctuation followed by whitespace or end-of-string.
parts = re.split(r'(?<=[.?!])\s+', q.strip())
first_sentence = parts[0] if parts else q
# Take the shorter of: first sentence, or first 80 chars (with ellipsis if cut).
if len(first_sentence) <= 80:
    print(first_sentence)
else:
    print(first_sentence[:77].rstrip() + '...')
" "$QUESTION" 2>/dev/null)
        timeout 3s "$PYTHON_BIN" -c "
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2.0)
    s.connect('$SOCKET_PATH')
    s.sendall(json.dumps({
        'command': 'speak',
        'text': sys.argv[1],
        'session_id': '$SESSION_ID',
        'priority': 9,
        'source': 'pre-tool-use-question',
    }).encode('utf-8'))
    s.close()
except Exception:
    pass
" "$QUESTION" 2>/dev/null &
    fi
    exit 0
fi

# Past here: Bash long-running heuristic only.
[[ "$TOOL_NAME" != "Bash" ]] && exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -z "$COMMAND" ]] && exit 0

# HOOK-03: Runner allowlist + word-boundary + operator stripping.
# Shell operators must not appear as announcement targets.
# ANNOUNCE-02: if no allowlist runner matches, exit SILENTLY — no generic fallback.
ANNOUNCE=$("$PYTHON_BIN" -c "
import re, sys

cmd = sys.argv[1]

# -----------------------------------------------------------------------
# Step 1: Strip after first unquoted shell operator so patterns only see
# the first command in a pipeline/chain. This prevents 'Running pytest on
# 2>&1' / 'Running tests on null;' false announcements.
# Split on unquoted: && || ; | > < 2>&1 2> &
# Simple approach: split on the first occurrence of these operator tokens.
_OP_RE = re.compile(r'\s*(?:&&|\|\||[|;&]|2>&1|2>|>|<)\s*')
cmd = _OP_RE.split(cmd, maxsplit=1)[0].strip()

# Strip leading shell preamble: env vars (KEY=val), sudo, exec, etc.
cmd = re.sub(r'^(?:[A-Z_]+=\S+\s+)+', '', cmd).strip()
cmd = re.sub(r'^(?:sudo|exec|time)\s+', '', cmd).strip()

if not cmd:
    sys.exit(0)

# -----------------------------------------------------------------------
# Deny-list: shell operator tokens must not appear in extracted target.
# If after stripping we still find operators (e.g., from quoted cmd), bail.
SHELL_OPS = frozenset(['&&', '||', ';', '|', '>', '<', '2>&1', '2>', '&'])

def is_operator(tok):
    return tok in SHELL_OPS or tok.startswith('>') or tok.startswith('<')

def short_target(tok):
    '''Reduce a path or filename to a concise spoken form.'''
    if not tok or is_operator(tok):
        return ''
    # Strip leading ./
    tok = re.sub(r'^\./', '', tok)
    # If it is a path, take the basename
    if '/' in tok:
        tok = tok.rsplit('/', 1)[-1] or tok
    # Strip common test extensions
    tok = re.sub(r'\.(py|js|ts|rs|go|sh|md)$', '', tok)
    # Strip leading test_ prefix for readability
    tok = re.sub(r'^test_', '', tok)
    return tok or ''

def safe_target_from_args(args_str):
    '''Extract first non-flag, non-operator token from an argument string.
    Returns empty string if none found or if token is a shell operator.
    '''
    for tok in args_str.split():
        if tok.startswith('-') or '=' in tok and '/' not in tok:
            continue
        if is_operator(tok):
            return ''
        return short_target(tok)
    return ''

# -----------------------------------------------------------------------
# ANNOUNCE-02: Only announce if an allowlisted runner is detected.
# Order matters: most specific patterns first.

# pytest
m = re.search(r'\bpytest\b\s*(.*)', cmd)
if m:
    args = m.group(1).strip()
    target = safe_target_from_args(args)
    if target:
        # If target looks like a directory (no extension), say 'in <dir>'
        if '.' not in target.split('/')[-1]:
            print(f'Running pytest in {target}.')
        else:
            print(f'Running pytest on {target}.')
    else:
        # No positional args — whole suite
        print('Running pytest.')
    sys.exit(0)

# cargo test / cargo build / cargo run / cargo check / cargo clippy
m = re.search(r'\bcargo\s+(test|build|run|check|clippy)\b(.*)', cmd)
if m:
    sub = m.group(1)
    args = m.group(2).strip()
    rel = ' release' if '--release' in cmd else ''
    if sub == 'test':
        # cargo test <module::path>
        m2 = re.search(r'\bcargo\s+test\s+([\w:]+)', cmd)
        if m2:
            print(f'Running cargo test {m2.group(1)}.')
        else:
            print('Running cargo tests.')
    else:
        print(f'Cargo {sub}{rel}.')
    sys.exit(0)

# npm test / npm run <script>
m = re.search(r'\bnpm\s+test\b(.*)', cmd)
if m:
    extra = m.group(1).strip()
    if '--watch' in extra or '-w' in extra:
        print('Running npm tests in watch mode.')
    else:
        print('Running npm tests.')
    sys.exit(0)
m = re.search(r'\bnpm\s+run\s+(\S+)', cmd)
if m:
    script = m.group(1)
    print(f'Running npm script {script}.')
    sys.exit(0)

# jest / vitest / mocha  (standalone invocations)
for runner in ('jest', 'vitest', 'mocha'):
    m = re.search(r'\b' + runner + r'\b\s*(.*)', cmd)
    if m:
        args = m.group(1).strip()
        target = safe_target_from_args(args)
        if target:
            print(f'Running {runner} on {target}.')
        else:
            print(f'Running {runner}.')
        sys.exit(0)

# go test
m = re.search(r'\bgo\s+test\b\s*(.*)', cmd)
if m:
    args = m.group(1).strip()
    m2 = re.search(r'(\.\.\.|\S+)', args)
    if m2:
        print(f'Running go test {m2.group(1)}.')
    else:
        print('Running go tests.')
    sys.exit(0)

# make <target>  — only if target looks test-like or is 'test'
m = re.search(r'\bmake\s+([\w./-]+)', cmd)
if m:
    target = m.group(1)
    # Only announce if target is 'test', 'tests', or contains 'test'
    if re.search(r'\btest', target, re.IGNORECASE):
        print(f'Running make {target}.')
        sys.exit(0)
    # Non-test make targets: announce only for well-known build targets
    if target in ('build', 'all', 'install', 'check', 'clean'):
        print(f'Running make {target}.')
        sys.exit(0)
    # Other make targets: silent
    sys.exit(0)

# ANNOUNCE-02: No allowlist match — exit silently (no generic fallback).
# Per ANNOUNCE-02: 'Running tests.' generic fallback is REMOVED.
sys.exit(0)
" "$COMMAND" 2>/dev/null)

[[ -z "$ANNOUNCE" ]] && exit 0

# Forward to daemon via the legacy `speak` socket command. Wave 3 W3.A
# kept this on the legacy path — see header comment for rationale.
# (SOCKET_PATH already declared at top.)
if [[ -S "$SOCKET_PATH" && -x "$PYTHON_BIN" ]]; then
    timeout 3s "$PYTHON_BIN" -c "
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect('$SOCKET_PATH')
    payload = json.dumps({
        'command': 'speak',
        'text': sys.argv[1],
        'session_id': '$SESSION_ID',
        'priority': 7,
        'source': 'pre-tool-use',
    })
    s.sendall(payload.encode('utf-8'))
    s.close()
except Exception:
    pass
" "$ANNOUNCE" 2>/dev/null &
fi

exit 0
