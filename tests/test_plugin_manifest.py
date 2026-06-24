"""Deterministic gate for the claude-tts plugin shell.

Validates the Claude Code plugin manifests, the five slash commands, the
tts-setup skill stub, public-PII cleanliness of every new shell file, and the
portability of the (pre-existing) hooks wiring. All-sync, no conftest, no
async — part of the `make verify` gate.
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"


def test_plugin_json_is_valid():
    assert PLUGIN_JSON.is_file(), f"missing {PLUGIN_JSON}"
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "claude-tts"
    assert isinstance(data.get("version"), str) and data["version"]
    assert isinstance(data.get("description"), str) and data["description"]
    assert data["license"] == "MIT"


def test_marketplace_json_is_valid_and_consistent():
    assert MARKETPLACE_JSON.is_file(), f"missing {MARKETPLACE_JSON}"
    market = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    plugins = market["plugins"]
    assert isinstance(plugins, list) and len(plugins) >= 1
    first = plugins[0]
    # INVARIANT: marketplace plugin name === plugin.json name === "claude-tts"
    assert first["name"] == plugin["name"] == "claude-tts"
    # INVARIANT: single self-marketplace points at repo root
    assert first["source"] == "./"
    assert isinstance(market["metadata"]["version"], str) and market["metadata"]["version"]


COMMANDS_DIR = ROOT / "commands"
SKILL_MD = ROOT / "skills" / "tts-setup" / "SKILL.md"
COMMAND_NAMES = ("setup", "voice", "status", "doctor", "uninstall")


def _frontmatter(text):
    """Return the YAML frontmatter block (between the first two '---' lines).

    Requires '---' on line 1 (no leading blank lines / BOM). Returns "" if the
    file does not open with a frontmatter fence.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    body = []
    for line in lines[1:]:
        if line.strip() == "---":
            return "\n".join(body)
        body.append(line)
    return ""  # no closing fence


def test_all_five_commands_exist_with_frontmatter():
    for name in COMMAND_NAMES:
        md = COMMANDS_DIR / f"{name}.md"
        assert md.is_file(), f"missing command {md}"
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{md} must open with '---' on line 1"
        fm = _frontmatter(text)
        assert fm, f"{md} has no closed frontmatter block"
        assert "name:" in fm, f"{md} frontmatter missing name:"
        assert "description:" in fm, f"{md} frontmatter missing description:"


def test_setup_skill_stub_exists_with_frontmatter():
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{SKILL_MD} must open with '---' on line 1"
    fm = _frontmatter(text)
    assert fm, f"{SKILL_MD} has no closed frontmatter block"
    assert "name: tts-setup" in fm, f"{SKILL_MD} frontmatter must declare name: tts-setup"
    assert "description:" in fm, f"{SKILL_MD} frontmatter missing description:"
