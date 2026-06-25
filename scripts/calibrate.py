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


from daemon.content_router import _drop_check_raw  # noqa: E402
from daemon.text_utils import is_speakable, normalize_for_speech  # noqa: E402


def floor_drops(text: str) -> bool:
    """True if the deterministic pipeline would DROP this before any model sees it.

    Mirrors the production gate order (see scripts/eval_model.floor_drops).
    """
    if _drop_check_raw(text):
        return True
    if not is_speakable(normalize_for_speech(text)):
        return True
    return False


async def score_calibration(provider, rows: list[dict]) -> dict:
    """Score a provider on `rows` using the production decision: floor AND judge.

    final_speak = (floor passes) AND (provider.judge == True), compared to label.
    Returns a result dict with confusion/precision/recall, or {"error": ...} if
    the provider is unreachable (any judge call raises).
    """
    tp = fp = tn = fn = 0
    try:
        for r in rows:
            text, label = r["text"], r["label"]
            if floor_drops(text):
                final_speak = False
            else:
                final_speak = bool(await provider.judge(text[:600], "Bash", ""))
            truth = label == "speak"
            if final_speak and truth:
                tp += 1
            elif final_speak and not truth:
                fp += 1
            elif not final_speak and not truth:
                tn += 1
            else:
                fn += 1
    except Exception as exc:  # unreachable backend / transport failure
        return {"error": repr(exc)}

    total = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    accuracy = (tp + tn) / total if total else 0.0
    return {
        "total": total,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "accuracy": round(accuracy, 4),
    }
