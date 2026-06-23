"""HOOK-01 + HOOK-02 invariants for hooks/hooks.json."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HOOKS_JSON = PROJECT_ROOT / "hooks" / "hooks.json"
HOOKS_DIR = PROJECT_ROOT / "hooks"


def _load_hooks_config() -> dict:
    with HOOKS_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_post_tool_use_has_exactly_one_hook_entry():
    """HOOK-01: exactly one PostToolUse hook entry registered."""
    cfg = _load_hooks_config()
    matchers = cfg.get("hooks", {}).get("PostToolUse", [])
    assert len(matchers) == 1, f"Expected 1 matcher, got {len(matchers)}"
    inner = matchers[0].get("hooks", [])
    assert len(inner) == 1, f"Expected 1 inner hook, got {len(inner)}: {inner}"


def test_no_references_to_deleted_scripts():
    """HOOK-02: every referenced script path exists on disk."""
    cfg = _load_hooks_config()
    raw = HOOKS_JSON.read_text(encoding="utf-8")
    for forbidden in ("post-assistant-message.sh", "tts-output-hook.sh"):
        assert forbidden not in raw, f"Dead script reference: {forbidden}"

    for matchers in cfg.get("hooks", {}).values():
        for matcher in matchers:
            for entry in matcher.get("hooks", []):
                cmd = entry.get("command", "")
                if "/hooks/" in cmd:
                    script_name = cmd.rsplit("/hooks/", 1)[-1].split()[0]
                    script_path = HOOKS_DIR / script_name
                    assert script_path.exists(), f"Missing: {script_path}"
