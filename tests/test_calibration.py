"""Gate tests for the setup-time calibration helpers (scripts/calibrate.py).

All-sync, no live model: load_oracle + select_mini_eval verified against the
real labeled oracle fixture. FakeProvider-driven scoring tests live below.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.calibrate import load_oracle, select_mini_eval  # noqa: E402

ORACLE = Path(__file__).resolve().parent / "fixtures" / "eval" / "labeled_oracle.jsonl"


def test_load_oracle_reads_all_rows():
    rows = load_oracle(ORACLE)
    assert len(rows) == 62
    assert all({"text", "label", "class"} <= set(r) for r in rows)


def test_mini_eval_keeps_every_boundary_row():
    rows = load_oracle(ORACLE)
    mini = select_mini_eval(rows, bulk_cap=8)
    classes = [r["class"] for r in mini]
    # The two small, load-bearing failure-boundary classes are kept in full.
    assert classes.count("recall_miss") == 6
    assert classes.count("precision_miss") == 7
    # Bulk classes are capped, so the subset is strictly smaller than the oracle.
    assert classes.count("gibberish") == 8
    assert classes.count("legit") == 8
    assert len(mini) == 29


def test_mini_eval_is_deterministic():
    rows = load_oracle(ORACLE)
    assert select_mini_eval(rows) == select_mini_eval(rows)


import asyncio  # noqa: E402

from scripts.calibrate import score_calibration  # noqa: E402


class FakeProvider:
    """In-memory LLMProvider stand-in. verdicts maps text -> judge() bool.

    Mirrors the seam signature: async judge(snippet, tool_name, context="") -> bool.
    """

    def __init__(self, verdicts: dict, raise_on=None):
        self._verdicts = verdicts
        self._raise_on = raise_on

    async def judge(self, snippet: str, tool_name: str, context: str = "") -> bool:
        if self._raise_on is not None:
            raise self._raise_on
        # snippet is text[:600]; match on the full provided snippet.
        return bool(self._verdicts.get(snippet, False))


def test_score_calibration_computes_precision_and_recall():
    # Two speak rows, two skip rows, all of which PASS the floor (real sentences).
    rows = [
        {"text": "23 passed, 4 failed in tests", "label": "speak", "class": "legit"},
        {"text": "Both imports OK without PYTHONPATH", "label": "speak", "class": "recall_miss"},
        {"text": "Running a long mechanical rsync of build artifacts now", "label": "skip", "class": "legit"},
        {"text": "Reticulating the splines for the widget cache layer", "label": "skip", "class": "legit"},
    ]
    # Judge says SPEAK for one true-speak (tp) and one true-skip (fp); SKIP otherwise.
    verdicts = {
        "23 passed, 4 failed in tests": True,
        "Running a long mechanical rsync of build artifacts now": True,
    }
    result = asyncio.run(score_calibration(FakeProvider(verdicts), rows))
    assert result["confusion"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert "error" not in result


def test_score_calibration_reports_unreachable_provider():
    rows = [{"text": "23 passed, 4 failed", "label": "speak", "class": "legit"}]
    provider = FakeProvider({}, raise_on=ConnectionError("refused"))
    result = asyncio.run(score_calibration(provider, rows))
    assert "error" in result
    assert "refused" in result["error"]


from scripts.calibrate import calibration_mode  # noqa: E402


def test_calibration_mode_smart_when_clean():
    result = {"precision": 1.0, "recall": 0.9, "accuracy": 0.95}
    assert calibration_mode(result) == "smart"


def test_calibration_mode_deterministic_on_low_precision():
    # Speaking gibberish (false-speaks) drops precision -> protect the 0%-gibberish win.
    result = {"precision": 0.7, "recall": 0.95, "accuracy": 0.85}
    assert calibration_mode(result) == "deterministic"


def test_calibration_mode_deterministic_on_low_recall():
    result = {"precision": 1.0, "recall": 0.5, "accuracy": 0.7}
    assert calibration_mode(result) == "deterministic"


def test_calibration_mode_deterministic_on_unreachable():
    assert calibration_mode({"error": "ConnectionError('refused')"}) == "deterministic"
