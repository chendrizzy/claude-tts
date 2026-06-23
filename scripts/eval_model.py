#!/usr/bin/env python3
"""Offline model-eval harness for the TTS LLM layer (Phase 0 of NEXT-SESSION-GOAL).

Scores an Ollama model on the labeled oracle WITHOUT a live daemon, by reusing
the EXACT production judge + summarizer code paths (``daemon.ollama_summarizer``),
so the eval can never drift from what production actually does.

Three things are measured, separated by *who controls them*:

  1. DETERMINISTIC FLOOR (model-invariant): ``_drop_check_raw`` + ``is_speakable``.
     - skip-labeled items the floor already catches  = free precision (any model).
     - speak-labeled items the floor WRONGLY kills    = recall killer; must be ~0.
  2. JUDGE (model-variant): the SPEAK/SKIP binary judge, parsed exactly like prod.
     End-to-end decision = (floor passes) AND (judge == SPEAK), scored vs label.
  3. SUMMARY FAITHFULNESS (model-variant): for legit items >= summarize_threshold,
     run the real summarizer and check number/filename retention + latency.

The oracle is CURATED and failure-weighted (it over-samples production recall/
precision misses on purpose), so precision/recall here are COMPARATIVE signals
between models on the decision boundary, not production rates. Pick the model
that (a) never raises floor false-drops, (b) maximizes judge accuracy, (c) keeps
faithfulness high at the lowest latency.

Usage:
  python3 scripts/eval_model.py --model llama3.2:3b
  python3 scripts/eval_model.py --model model --model llama3.2:3b --json
  python3 scripts/eval_model.py --selftest     # no network; floor sanity only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from daemon.tts_types import Category  # noqa: E402
from daemon.text_utils import is_speakable, normalize_for_speech  # noqa: E402
from daemon.content_router import _drop_check_raw  # noqa: E402

DEFAULT_ORACLE = REPO / "tests" / "fixtures" / "eval" / "labeled_oracle.jsonl"
SUMMARY_ORACLE = REPO / "tests" / "fixtures" / "eval" / "summary_oracle.jsonl"
SUMMARIZE_THRESHOLD = 120  # mirrors content_router.SUMMARIZE_THRESHOLD_CHARS

# The judge prompt's context_hint, copied verbatim from ContentRouter._binary_llm_judge
# with the optional "Context:" clause empty (oracle texts carry no originating event,
# so the bare judge is the fair common denominator across candidate models).
JUDGE_CONTEXT_HINT = (
    "BINARY_JUDGMENT: tool=Bash. "
    "Reply with exactly 'SPEAK' or 'SKIP' — nothing else. "
    "SPEAK if a TTS readout would surface a meaningful finding "
    "(test counts, error messages, useful numbers, status pivots) "
    "relevant to the active work above. "
    "SKIP if it is mechanical noise or repeats output already spoken."
)

# ----------------------------------------------------------------------------
# Deterministic floor (model-invariant) — replicate the production gate order.
# ----------------------------------------------------------------------------
def floor_drops(text: str) -> bool:
    """True if the deterministic pipeline would DROP this before any model sees it."""
    if _drop_check_raw(text):  # returns a reason string when it should drop
        return True
    if not is_speakable(normalize_for_speech(text)):
        return True
    return False


# ----------------------------------------------------------------------------
# Judge (model-variant) — exact production parse.
# ----------------------------------------------------------------------------
async def judge_speak(summarizer, text: str) -> tuple[bool, str | None, float]:
    snippet = text[:600]
    t0 = time.monotonic()
    verdict = await summarizer.summarize(
        snippet, Category.STATUS, JUDGE_CONTEXT_HINT, allow_fallback=False
    )
    dt = (time.monotonic() - t0) * 1000.0
    if not verdict:
        return False, verdict, dt
    token = verdict.strip().split()[0].upper().rstrip(".,!?:")
    return token == "SPEAK", verdict, dt


async def eval_model(model: str, rows: list[dict], do_summary: bool) -> dict:
    from daemon.ollama_integration import OllamaClient
    from daemon.ollama_summarizer import OllamaSummarizer

    summarizer = OllamaSummarizer(OllamaClient(), model=model, timeout_s=20.0)

    # Warm the model once so the cold-load outlier doesn't pollute latency stats.
    await summarizer.summarize("ok", Category.STATUS, JUDGE_CONTEXT_HINT, allow_fallback=False)

    tp = fp = tn = fn = 0
    floor_false_drops: list[str] = []   # speak-labeled items the floor kills (recall killer)
    floor_catches = 0                   # skip-labeled items the floor handles model-free
    judge_correct = judge_total = 0     # judge accuracy on items that REACH the judge
    latencies: list[float] = []
    by_class: dict[str, dict[str, int]] = {}
    errors: list[str] = []

    for r in rows:
        text, label, klass = r["text"], r["label"], r["class"]
        by_class.setdefault(klass, {"n": 0, "correct": 0})
        by_class[klass]["n"] += 1

        dropped = floor_drops(text)
        if dropped:
            final_speak = False
            if label == "skip":
                floor_catches += 1
            else:
                floor_false_drops.append(text[:70])
        else:
            jspeak, verdict, dt = await judge_speak(summarizer, text)
            latencies.append(dt)
            if verdict is None:
                errors.append(text[:50])
            judge_total += 1
            # judge "correct" = its verdict matches the label for an item it controls
            if (jspeak and label == "speak") or (not jspeak and label == "skip"):
                judge_correct += 1
            final_speak = jspeak

        # end-to-end confusion vs label
        if label == "speak" and final_speak:
            tp += 1
        elif label == "speak" and not final_speak:
            fn += 1
        elif label == "skip" and final_speak:
            fp += 1
        else:
            tn += 1

        correct = (final_speak and label == "speak") or (not final_speak and label == "skip")
        if correct:
            by_class[klass]["correct"] += 1

    total = len(rows)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    accuracy = (tp + tn) / total if total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    summary_stats = None
    if do_summary and SUMMARY_ORACLE.exists():
        srows = [json.loads(l) for l in open(SUMMARY_ORACLE) if l.strip()]
        rets: list[float] = []
        slat: list[float] = []
        items: list[dict] = []
        for r in srows:
            cat = Category(r.get("category", "status"))
            t0 = time.monotonic()
            out = (await summarizer.summarize(r["text"], cat, r.get("context_hint", "(none)"))) or ""
            dt = (time.monotonic() - t0) * 1000.0
            slat.append(dt)
            must = r.get("must_keep", [])
            kept = sum(1 for k in must if k.lower() in out.lower())
            ret = kept / len(must) if must else None
            if ret is not None:
                rets.append(ret)
            items.append({
                "category": r.get("category"), "out": out.strip(),
                "retention": round(ret, 2) if ret is not None else None, "ms": round(dt),
            })
        summary_stats = {
            "n": len(srows),
            "mean_retention": round(statistics.mean(rets), 3) if rets else None,
            "median_latency_ms": round(statistics.median(slat)) if slat else None,
            "items": items,
        }

    return {
        "model": model,
        "total": total,
        "accuracy": round(accuracy, 3),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "floor": {
            "skip_items_caught_model_free": floor_catches,
            "speak_items_wrongly_killed": len(floor_false_drops),
            "wrongly_killed_examples": floor_false_drops,
        },
        "judge": {
            "items_reaching_judge": judge_total,
            "accuracy_on_reached": round(judge_correct / judge_total, 3) if judge_total else None,
            "empty_verdicts": len(errors),
        },
        "latency_ms": {
            "median": round(statistics.median(latencies)) if latencies else None,
            "p90": round(sorted(latencies)[int(len(latencies) * 0.9)]) if latencies else None,
        },
        "summary": summary_stats,
        "by_class": by_class,
    }


def print_report(res: dict) -> None:
    print(f"\n{'='*64}\nMODEL: {res['model']}   (n={res['total']})\n{'='*64}")
    c = res["confusion"]
    print(f"  end-to-end: acc={res['accuracy']}  precision={res['precision']}  "
          f"recall={res['recall']}  f1={res['f1']}")
    print(f"  confusion : TP={c['tp']} FP={c['fp']} TN={c['tn']} FN={c['fn']}")
    fl = res["floor"]
    flag = "  <-- RECALL KILLER" if fl["speak_items_wrongly_killed"] else ""
    print(f"  floor     : caught {fl['skip_items_caught_model_free']} skips model-free; "
          f"wrongly killed {fl['speak_items_wrongly_killed']} speak items{flag}")
    if fl["wrongly_killed_examples"]:
        for ex in fl["wrongly_killed_examples"]:
            print(f"              ! {ex!r}")
    j = res["judge"]
    print(f"  judge     : reached {j['items_reaching_judge']} items, "
          f"acc_on_reached={j['accuracy_on_reached']}, empty_verdicts={j['empty_verdicts']}")
    lat = res["latency_ms"]
    print(f"  latency   : judge median={lat['median']}ms p90={lat['p90']}ms")
    if res["summary"]:
        s = res["summary"]
        print(f"  summary   : n={s['n']} mean_fact_retention={s['mean_retention']} "
              f"median_latency={s['median_latency_ms']}ms")
        for it in s.get("items", []):
            print(f"      [{it['category']:12} ret={it['retention']} {it['ms']}ms] {it['out']!r}")
    print("  by class  :")
    for k, v in res["by_class"].items():
        print(f"      {k:14} {v['correct']}/{v['n']}")


def selftest() -> None:
    """No-network sanity: the floor must kill obvious gibberish and pass real prose."""
    assert floor_drops("0"), "bare number should be floor-dropped"
    assert floor_drops("8f790dda64465fd8bb0a7a3c3e62eb02a2826cd4"), "hash should drop"
    assert not floor_drops("23 passed, 4 failed."), "real test result must survive"
    assert not floor_drops("Both imports OK without PYTHONPATH"), "recall_miss must survive"
    print("selftest OK")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", action="append", default=[], help="Ollama model id (repeatable)")
    ap.add_argument("--oracle", default=str(DEFAULT_ORACLE))
    ap.add_argument("--no-summary", action="store_true", help="skip summary-faithfulness pass")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.model:
        ap.error("provide at least one --model (or --selftest)")

    rows = [json.loads(l) for l in open(args.oracle) if l.strip()]
    results = []
    for m in args.model:
        try:
            results.append(asyncio.run(eval_model(m, rows, do_summary=not args.no_summary)))
        except Exception as exc:  # noqa: BLE001
            results.append({"model": m, "error": repr(exc)})

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for res in results:
            if "error" in res:
                print(f"\nMODEL {res['model']}: ERROR {res['error']}")
            else:
                print_report(res)


if __name__ == "__main__":
    main()
