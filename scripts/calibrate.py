#!/usr/bin/env python3
"""Setup-time calibration for /tts:setup.

Loads the labeled oracle, scores the chosen LLM provider against a stratified
mini-subset (the production floor AND the provider's judge), and decides smart
mode (use the LLM) vs deterministic mode (deterministic floor only). Pure
selection/scoring/decision logic is unit-tested in `make verify`; the live
provider call runs only at setup time.

Usage:
  python3 scripts/calibrate.py --backend ollama --model qwen2.5-coder:1.5b --json
  python3 scripts/calibrate.py --backend openai --base-url URL --model M --api-key-env KEY
  python3 scripts/calibrate.py --backend null
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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


# Defaults are conservative and gibberish-protective: precision guards against
# speaking noise (the shipped 0%-gibberish win); recall guards against muting
# real findings. Tunable, but these are the safe floor for "is the model better
# than the deterministic baseline?".
MIN_PRECISION = 0.9
MIN_RECALL = 0.8


def calibration_mode(result: dict, *, min_precision: float = MIN_PRECISION,
                     min_recall: float = MIN_RECALL) -> str:
    """'smart' if the model is reachable and clears both bars, else 'deterministic'."""
    if "error" in result:
        return "deterministic"
    if result.get("precision", 0.0) < min_precision:
        return "deterministic"
    if result.get("recall", 0.0) < min_recall:
        return "deterministic"
    return "smart"


def build_provider(backend: str, *, model: str = "", base_url: str = "",
                   api_key: str = "", timeout_s: float = 8.0):
    """Construct an LLMProvider for the chosen backend. No network at construction."""
    from daemon.providers.null_provider import NullProvider
    backend = backend.lower()
    if backend == "null":
        return NullProvider()
    if backend == "openai":
        from daemon.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(base_url=base_url, model=model,
                                    api_key=api_key, timeout_s=timeout_s)
    # default: ollama
    from daemon.ollama_integration import OllamaClient
    from daemon.ollama_summarizer import OllamaSummarizer
    from daemon.providers.ollama_provider import OllamaProvider
    summarizer = OllamaSummarizer(OllamaClient(), model=model, timeout_s=timeout_s)
    return OllamaProvider(summarizer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Calibrate the TTS LLM backend for setup.")
    ap.add_argument("--backend", required=True, choices=["ollama", "openai", "null"])
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key-env", default="",
                    help="env var holding the API key (never pass the key on argv)")
    ap.add_argument("--oracle", default=str(DEFAULT_ORACLE))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.backend == "null":
        # No model to score; deterministic mode is the definition of 'null'.
        print(json.dumps({"mode": "deterministic", "reason": "no-llm backend"}))
        return 0

    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    provider = build_provider(args.backend, model=args.model,
                              base_url=args.base_url, api_key=api_key)
    rows = select_mini_eval(load_oracle(Path(args.oracle)))
    result = asyncio.run(score_calibration(provider, rows))
    mode = calibration_mode(result)
    out = {"mode": mode, **result}
    print(json.dumps(out, indent=2) if args.json else json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
