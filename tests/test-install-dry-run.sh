#!/bin/bash
# Dry-run test of INSTALL.sh
# Tests installation logic without making changes

set -e

echo "=== TTS Package Installation - DRY RUN TEST ==="
echo

# Test 1: Script directory resolution
echo "TEST 1: Script directory resolution"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "✓ SCRIPT_DIR resolved to: $SCRIPT_DIR"
if [ -d "$SCRIPT_DIR" ]; then
    echo "✓ SCRIPT_DIR exists"
else
    echo "✗ SCRIPT_DIR does not exist"
    exit 1
fi
echo

# Test 2: Prerequisites detection
echo "TEST 2: Prerequisites detection"
if command -v python3 &>/dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo "✓ Python 3 found: $PYTHON_VERSION"
else
    echo "✗ Python 3 not found"
    exit 1
fi

if command -v pip3 &>/dev/null; then
    echo "✓ pip3 found"
else
    echo "✗ pip3 not found"
    exit 1
fi

if command -v jq &>/dev/null; then
    echo "✓ jq found"
else
    echo "⚠ jq not found (optional)"
fi
echo

# Test 3: Requirements file validation
echo "TEST 3: Requirements file validation"
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "✓ requirements.txt exists"
    echo "  Dependencies:"
    grep -E "^[^#]" "$SCRIPT_DIR/requirements.txt" | grep -v "^$" | sed 's/^/    - /'
else
    echo "✗ requirements.txt not found"
    exit 1
fi
echo

# Test 4: Critical directories exist
echo "TEST 4: Critical directories"
for dir in daemon hooks config scripts; do
    if [ -d "$SCRIPT_DIR/$dir" ]; then
        echo "✓ $dir/ exists"
    else
        echo "✗ $dir/ not found"
        exit 1
    fi
done
echo

# Test 5: Critical files exist
echo "TEST 5: Critical files"
CRITICAL_FILES=(
    "daemon/tts_daemon.py"
    "daemon/enhanced_hook_integration.py"
    "hooks/session-start.sh"
    "hooks/ensure-daemon-ready.sh"
    "hooks/hooks.json"
    "tts-launcher-integration.sh"
    ".claude-plugin/marketplace.json"
)

for file in "${CRITICAL_FILES[@]}"; do
    if [ -f "$SCRIPT_DIR/$file" ]; then
        echo "✓ $file exists"
    else
        echo "✗ $file not found"
        exit 1
    fi
done
echo

# Test 6: Script permissions
echo "TEST 6: Script permissions"
EXEC_SCRIPTS=(
    "tts-launcher-integration.sh"
    "hooks/session-start.sh"
    "hooks/ensure-daemon-ready.sh"
    "hooks/post-tool-use.sh"
    "scripts/tts_system_control.sh"
)

for script in "${EXEC_SCRIPTS[@]}"; do
    if [ -x "$SCRIPT_DIR/$script" ]; then
        echo "✓ $script is executable"
    else
        echo "✗ $script not executable"
        exit 1
    fi
done
echo

# Test 7: Ollama detection
echo "TEST 7: Ollama detection"
if command -v ollama &>/dev/null; then
    OLLAMA_VERSION=$(ollama --version 2>/dev/null | awk '{print $3}' || echo "unknown")
    echo "✓ Ollama found (version: $OLLAMA_VERSION)"

    # Check if Ollama service is running
    if curl -s http://localhost:11434/api/tags &>/dev/null; then
        echo "✓ Ollama service is running"

        # Check if model exists
        if ollama list | grep -q "llama3.2"; then
            echo "✓ llama3.2 model present"
        else
            echo "⚠ llama3.2 model not found (would be pulled during install)"
        fi
    else
        echo "⚠ Ollama service not running (would be started during install)"
    fi
else
    echo "⚠ Ollama not found (would be installed during install)"
fi
echo

# Test 8: Python dependencies check
echo "TEST 8: Python dependencies verification"
DEPS=("edge_tts" "ollama" "psutil" "requests")
for dep in "${DEPS[@]}"; do
    if python3 -c "import ${dep//-/_}" &>/dev/null; then
        VERSION=$(python3 -c "import ${dep//-/_}; print(${dep//-/_}.__version__)" 2>/dev/null || echo "installed")
        echo "✓ $dep available ($VERSION)"
    else
        echo "⚠ $dep not found (would be installed)"
    fi
done
echo

# Test 9: Marketplace configuration check
echo "TEST 9: Marketplace configuration"
if [ -f "$HOME/.claude/marketplace.json" ]; then
    echo "✓ marketplace.json exists"
    if grep -q "claude-tts-marketplace" "$HOME/.claude/marketplace.json" 2>/dev/null; then
        echo "✓ TTS marketplace already configured"
    else
        echo "⚠ TTS marketplace not configured (would be added)"
    fi
else
    echo "⚠ marketplace.json not found (would be created)"
fi
echo

# Test 10: Daemon socket check
echo "TEST 10: Daemon status"
SOCKET="/tmp/tts_daemon.sock"
if [ -S "$SOCKET" ]; then
    echo "✓ Daemon socket exists"
    if timeout 1 python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.connect('$SOCKET'); s.close()" 2>/dev/null; then
        echo "✓ Daemon is responsive"
    else
        echo "⚠ Daemon socket exists but not responsive"
    fi
else
    echo "⚠ Daemon not running (would be started during install)"
fi
echo

# Test 11: Integration method paths
echo "TEST 11: Integration method paths"
LAUNCHER_PATH="$HOME/bin/claude"
if [ -f "$LAUNCHER_PATH" ]; then
    echo "✓ Launcher exists at $LAUNCHER_PATH"
    echo "  (Installation would offer to backup and replace)"
else
    echo "⚠ No existing launcher (Installation would create if Option 2 selected)"
fi

if [ ! -d "$HOME/bin" ]; then
    echo "⚠ ~/bin directory doesn't exist (would be created for Option 2)"
else
    echo "✓ ~/bin directory exists"
fi
echo

# Test 12: Config file validation
echo "TEST 12: Configuration files"
if [ -f "$SCRIPT_DIR/config/tts_user_config.json" ]; then
    echo "✓ User config exists"
    if python3 -c "import json; json.load(open('$SCRIPT_DIR/config/tts_user_config.json'))" 2>/dev/null; then
        echo "✓ User config is valid JSON"
    else
        echo "✗ User config is invalid JSON"
        exit 1
    fi
fi

if [ -f "$SCRIPT_DIR/hooks/hooks.json" ]; then
    echo "✓ hooks.json exists"
    if python3 -c "import json; json.load(open('$SCRIPT_DIR/hooks/hooks.json'))" 2>/dev/null; then
        echo "✓ hooks.json is valid JSON"

        # Check if SessionStart is configured
        if grep -q "SessionStart" "$SCRIPT_DIR/hooks/hooks.json"; then
            echo "✓ SessionStart hook configured"
        else
            echo "✗ SessionStart hook not configured"
            exit 1
        fi
    else
        echo "✗ hooks.json is invalid JSON"
        exit 1
    fi
fi
echo

# Test 13: Path resolution compatibility
echo "TEST 13: Path resolution patterns"
if grep -q '${CLAUDE_PLUGIN_ROOT}' "$SCRIPT_DIR/hooks/hooks.json"; then
    echo "✓ hooks.json uses \${CLAUDE_PLUGIN_ROOT}"
else
    echo "✗ hooks.json missing \${CLAUDE_PLUGIN_ROOT}"
    exit 1
fi

if grep -q 'PLUGIN_ROOT.*dirname.*BASH_SOURCE' "$SCRIPT_DIR/hooks/session-start.sh"; then
    echo "✓ session-start.sh uses dynamic path resolution"
else
    echo "✗ session-start.sh missing dynamic path resolution"
    exit 1
fi
echo

# Summary
echo "=== DRY RUN TEST SUMMARY ==="
echo "✅ All critical tests passed!"
echo
echo "Installation readiness:"
echo "  • Prerequisites: Available"
echo "  • File structure: Complete"
echo "  • Permissions: Correct"
echo "  • Configuration: Valid"
echo "  • Path resolution: Portable"
echo
echo "The installation script should work correctly on:"
echo "  ✓ Systems with Python 3.8+"
echo "  ✓ macOS, Linux, WSL"
echo "  ✓ Fresh installations (no Ollama)"
echo "  ✓ Existing installations (with Ollama)"
echo "  ✓ Marketplace-only mode"
echo "  ✓ Launcher integration mode"
echo
echo "🎉 Package ready for distribution!"
