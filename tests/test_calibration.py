"""Gate tests for the setup-time calibration helpers (scripts/calibrate.py).

All-sync, no live model: load_oracle + select_mini_eval verified against the
real labeled oracle fixture. Later steps add FakeProvider-driven scoring tests.
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
