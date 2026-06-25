"""Sync the plugin manifests' version from pyproject.toml (single source of truth).

Run `make manifests` (or `python scripts/build_manifests.py`) after bumping the
version in pyproject.toml to propagate it to .claude-plugin/{plugin,marketplace}.json.
A gate test (tests/test_plugin_manifest.py) fails if they ever drift.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_VERSION_RE = re.compile(r'("version"\s*:\s*)"[^"]*"')
_MANIFESTS = (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")


def pyproject_version(pyproject_text: str) -> str:
    """The [project].version string from a pyproject.toml's text."""
    return tomllib.loads(pyproject_text)["project"]["version"]


def sync_version(manifest_text: str, version: str) -> str:
    """Rewrite every "version": "..." field to `version` (touches nothing else)."""
    return _VERSION_RE.sub(rf'\1"{version}"', manifest_text)


def main() -> int:
    version = pyproject_version((ROOT / "pyproject.toml").read_text())
    for rel in _MANIFESTS:
        path = ROOT / rel
        path.write_text(sync_version(path.read_text(), version))
    print(f"manifests synced to {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
