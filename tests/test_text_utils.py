"""Tests for daemon/text_utils.py — path humanization for TTS."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.text_utils import humanize_paths, normalize_for_speech  # noqa: E402


@pytest.mark.parametrize("raw,keep", [
    # Complete inline link still strips to its text (regression guard).
    ("See [the guide](https://example.com/guide) for details.", "the guide"),
    # Truncated/dangling links — a shadow.log excerpt cut mid-URL (no closing
    # paren) must NOT leave a dangling '](' that reaches the voice and trips the
    # R1 shadow-replay gate. See text_utils._LINK_INLINE_RE.
    ("Start with [How to read a node entry](#how-to-read-a-", "How to read a node entry"),
    ("Start with [How to read a node entry](#how-to-read-a-…", "How to read a node entry"),
])
def test_normalize_strips_links_including_truncated(raw: str, keep: str) -> None:
    out = normalize_for_speech(raw)
    assert "](" not in out, f"dangling link markup survived: {out!r}"
    assert keep in out


@pytest.mark.parametrize("raw,expected", [
    # The user's exact example
    ("/Volumes/DISK/GitHub/music/example", "music/example"),
    # User-home path under /Users/X/
    ("/home/user/project/daemon/types.py", "daemon/types.py"),
    # Home-relative
    ("~/projects/web/api/server.ts", "api/server.ts"),
    # ./ relative
    ("./tests/fixtures/event_corpus.jsonl", "fixtures/event_corpus.jsonl"),
    # ../ relative
    ("../sibling-pkg/lib/util.go", "lib/util.go"),
    # Bare relative paths (no leading / or ./) are NOT touched — too risky
    # to detect without false positives on prose like "either/or" or "and/or".
    ("src/foo/bar.py", "src/foo/bar.py"),
    # node_modules / __pycache__ / dist get dropped from middle
    ("/Users/x/proj/node_modules/react/index.js", "react/index.js"),
    ("/Users/x/.git/HEAD", "HEAD"),
    # /private/var noise stripped
    ("/private/var/folders/x/y/T/tmp.mp3", "T/tmp.mp3"),
    # Single-segment paths left alone (read fine as-is)
    ("/tmp", "/tmp"),
    # URLs untouched (no "://" eaten)
    ("Visit http://example.com/foo/bar for docs.",
     "Visit http://example.com/foo/bar for docs."),
])
def test_humanize_paths(raw: str, expected: str) -> None:
    assert humanize_paths(raw) == expected


def test_humanize_paths_in_sentence() -> None:
    """Embedded paths in prose get rewritten in place."""
    text = "Found the bug in /home/user/project/daemon/types.py at line 42."
    out = humanize_paths(text)
    assert "Found the bug in" in out
    assert "daemon/types.py" in out
    assert "/Users/" not in out
    assert "at line 42" in out


def test_humanize_paths_empty_and_no_slash() -> None:
    """Edge cases: empty, no path, None-safe."""
    assert humanize_paths("") == ""
    assert humanize_paths("Just plain text without any paths.") == \
        "Just plain text without any paths."


def test_humanize_paths_multiple_paths_in_one_string() -> None:
    """All path-like tokens get rewritten independently."""
    text = ("Compared /Volumes/DISK/GitHub/music/example to "
            "/Users/user/projects/audio.")
    out = humanize_paths(text)
    assert "music/example" in out
    assert "projects/audio" in out
    assert "DISK" not in out
    assert "/Users/" not in out


# --- Linux/CI dictionary fallback (bundled public-domain word list) ----------
# Regression for the CI-caught macOS/Linux divergence: is_speakable's
# zero-real-word noise gate is conditional on a populated _SYSTEM_DICT. Linux/CI
# hosts ship no /usr/share/dict/words, so the gate was silently disabled there
# and noise like "agent- agent- agent-" was wrongly KEPT (spoken). The bundle
# makes the dict present on every platform.
import daemon.text_utils as _tu  # noqa: E402


def test_bundled_dict_loads_and_has_common_words():
    d = _tu._load_bundled_dict()
    assert len(d) > 100000, "bundled word list should be comprehensive"
    for w in ("build", "error", "agent", "race", "condition"):
        assert w in d, f"bundled dict missing common word: {w}"
    assert "agent-" not in d  # a trailing-hyphen fragment is never a real word


def test_system_dict_is_never_empty():
    # Must be populated on EVERY platform (OS list on macOS, bundle on Linux/CI);
    # an empty dict silently disables the zero-real-word noise drop.
    assert len(_tu._SYSTEM_DICT) > 0


def test_zero_real_word_noise_drops_with_bundled_dict(monkeypatch):
    # Simulate a host without /usr/share/dict/words: force the bundled dict and
    # confirm the agent-id dump is still dropped (the exact CI failure on Linux).
    monkeypatch.setattr(_tu, "_SYSTEM_DICT", _tu._load_bundled_dict())
    assert _tu.is_speakable("agent- agent- agent-") is False
