#!/usr/bin/env python3
"""Setup-time calibration helpers for /tts:setup.

Loads the labeled oracle and selects a stratified mini-subset for calibration.
Provider scoring and the smart-vs-deterministic decision are added in later
steps; the pure selection logic here is unit-tested in `make verify`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_ORACLE = REPO / "tests" / "fixtures" / "eval" / "labeled_oracle.jsonl"

# Failure-boundary classes are kept in full; bulk classes are down-sampled.
BOUNDARY_CLASSES = ("recall_miss", "precision_miss")


def load_oracle(path: Path = DEFAULT_ORACLE) -> list[dict]:
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def select_mini_eval(rows: list[dict], *, bulk_cap: int = 8) -> list[dict]:
    """Stratified subset: keep every boundary-class row, cap each bulk class.

    Order-preserving and deterministic (no shuffling) so the gate test and the
    setup run agree exactly.
    """
    out: list[dict] = []
    seen: dict[str, int] = {}
    for r in rows:
        klass = r["class"]
        if klass in BOUNDARY_CLASSES:
            out.append(r)
            continue
        n = seen.get(klass, 0)
        if n < bulk_cap:
            out.append(r)
            seen[klass] = n + 1
    return out
