"""Shadow-log replay — property test of R1 normalization over REAL decisions.

The daemon logs every routing decision to ~/.claude/logs/tts/shadow.log. ~23% of
`should_speak` excerpts carry raw markdown (reproduced in the 2026-06-03
diagnosis). This test runs every real `should_speak` excerpt through
normalize_for_speech() and asserts the unambiguous-markup leak rate drops to
ZERO — proving R1 fixes production data, not just hand-written fixtures.

Skips cleanly when shadow.log is absent (CI/headless), so it never blocks the
gate on a machine without the live corpus.

Run:  python3 -m pytest -q tests/test_shadow_replay.py -s
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from daemon.text_utils import normalize_for_speech
    _HAVE_NORMALIZE = True
except Exception:
    _HAVE_NORMALIZE = False

    def normalize_for_speech(t: str) -> str:  # type: ignore
        return t or ""


def _shadow_path() -> Path:
    base = os.environ.get("CLAUDE_TTS_LOG_DIR")
    root = Path(base) if base else Path.home() / ".claude" / "logs" / "tts"
    return root / "shadow.log"


# Unambiguous markup that must NEVER survive normalization. Deliberately
# excludes lone '|' (shell pipes) and lone '*'/'_' (globs, snake_case) which
# are legitimately preserved.
_MARKUP_PATTERNS = [
    ("bold", re.compile(r"(?<![A-Za-z0-9])\*\*|\*\*(?![A-Za-z0-9])")),
    ("atx_header", re.compile(r"(?m)^\s{0,3}#{1,6}\s")),
    ("backtick", re.compile(r"`")),
    ("box_drawing", re.compile(r"[─-╿▀-▟☀-⛿★☆]")),
    ("md_link", re.compile(r"\]\(")),
    ("strike", re.compile(r"~~")),
    # --- code/programmatic-syntax gibberish (R5): proven 0% on real shadow.log.
    ("eq_run", re.compile(r"={2,}")),
    ("neq_op", re.compile(r"!==?")),
    ("logic_op", re.compile(r"&&|\|\|")),
    ("arrow_op", re.compile(r"=>|->")),
    ("diff_hunk", re.compile(r"@@[^@\n]*@@")),
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("hex_color", re.compile(r"#[0-9a-fA-F]{6}")),
    ("hex_hash", re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]{7,40}\b")),
    ("lone_letter_run", re.compile(r"(?:(?<=\s)|^)([b-hj-z])(?: \1){2,}(?=\s|$)")),
]


def _has_markup(text: str) -> bool:
    return any(p.search(text) for _, p in _MARKUP_PATTERNS)


def _load_should_speak_excerpts(path: Path, limit: int = 50000) -> list[str]:
    excerpts = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            nd = obj.get("new_decision")
            if isinstance(nd, dict) and nd.get("should_speak"):
                ex = nd.get("raw_excerpt") or ""
                if ex:
                    excerpts.append(ex)
            if len(excerpts) >= limit:
                break
    return excerpts


@pytest.mark.skipif(not _HAVE_NORMALIZE, reason="normalize_for_speech not available")
def test_shadow_corpus_markup_eliminated(capsys):
    path = _shadow_path()
    if not path.exists():
        pytest.skip(f"no shadow.log at {path} (headless/CI)")

    excerpts = _load_should_speak_excerpts(path)
    if not excerpts:
        pytest.skip("shadow.log has no should_speak excerpts")

    before = [e for e in excerpts if _has_markup(e)]
    after_offenders = []
    for e in excerpts:
        rendered = normalize_for_speech(e)
        if _has_markup(rendered):
            after_offenders.append((e, rendered))

    total = len(excerpts)
    before_rate = 100.0 * len(before) / total
    after_rate = 100.0 * len(after_offenders) / total

    # Evidence (visible with -s):
    with capsys.disabled():
        print(
            f"\n[shadow-replay] {total} should_speak excerpts | "
            f"markup BEFORE = {len(before)} ({before_rate:.1f}%) | "
            f"markup AFTER  = {len(after_offenders)} ({after_rate:.2f}%)"
        )
        for raw, rendered in after_offenders[:5]:
            print(f"  LEAK raw={raw[:90]!r}\n       out={rendered[:90]!r}")

    assert not after_offenders, (
        f"{len(after_offenders)}/{total} excerpts still carry markup after "
        f"normalization (was {len(before)} before). First offenders:\n"
        + "\n".join(f"  raw={r[:120]!r}\n  out={o[:120]!r}" for r, o in after_offenders[:5])
    )
