"""ContentRouter._drop_check prose mode — the turn-summary recall fix.

stop_events are the assistant's end-of-turn PROSE summaries, not tool stdout.
Mid-content noise patterns (markdown horizontal rules `-----`, "N files changed",
commit SHAs) false-positive on ordinary markdown in prose and veto the WHOLE
summary — measured ~29% of real turn summaries were lost this way. With
prose=True those MID-CONTENT patterns are skipped.

But WHOLE-MESSAGE shape stays dropped even as prose: a bare pasted code block or
a bare file path is not a summary. And empty / system-reminder / boilerplate /
dedup always apply. Tool paths (prose=False default) are unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from daemon.content_router import ContentRouter


def _router():
    return ContentRouter(config={})


def test_prose_keeps_summary_with_markdown_rule():
    r = _router()
    summary = (
        "Final status:\n\n## Done\n--------\n"
        "Shipped the disk guard; 3 files changed.\n"
        "All green."
    )
    assert r._drop_check(summary, prose=True) is None        # prose: spoken
    assert r._drop_check(summary, prose=False) is not None    # tool output: dropped


def test_mid_content_patterns_pass_as_prose_drop_as_tool():
    r = _router()
    for txt in ("Progress so far:\n--------\nmore detail", "Section ====== complete"):
        assert r._drop_check(txt, prose=True) is None, txt
        assert r._drop_check(txt, prose=False) is not None, txt


def test_whole_message_shape_still_drops_even_as_prose():
    r = _router()
    # A bare code block or bare path is NOT a turn summary — stays silent.
    assert r._drop_check("```python\nx=1\n```", prose=True) is not None
    assert r._drop_check("src/foo/bar.py", prose=True) is not None


def test_prose_still_drops_empty_systemreminder_and_dupes():
    r = _router()
    assert r._drop_check("", prose=True) == "empty content"
    assert r._drop_check("<system-reminder>hi</system-reminder>", prose=True) is not None
    # dedup MUST stay on for prose — Stop hook re-sends identical final text.
    r._note_hash("a one-off summary sentence", "s1")
    assert r._drop_check("a one-off summary sentence", prose=True) == "duplicate of recent content"


def test_tool_path_unchanged_with_default_prose_false():
    r = _router()
    assert r._drop_check("--------") is not None
    assert r._drop_check("```\ncode\n```") is not None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
