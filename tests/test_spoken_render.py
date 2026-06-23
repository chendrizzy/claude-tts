"""Spoken-text rendering harness — the quality oracle (DIAGNOSIS R1 / H0).

This is the regression gate the project lacked: a golden corpus of
`raw -> expected_spoken` contracts that FAILS on markdown leakage, the
inline-code deletion bug, summary truncation artifacts, and that PROVES the
normalizer is idempotent and does not corrupt legitimate content (shell pipes,
snake_case, globs).

Source of truth: tests/fixtures/spoken_corpus/cases.jsonl (regenerate with
`python3 tests/fixtures/spoken_corpus/_generate.py`).

Run:  python3 -m pytest -q tests/test_spoken_render.py
Gate: `make verify`

Design note: imports are defensive so that BEFORE the R1 fix this file produces
a meaningful RED (assertions fail showing markup leaks) rather than a collection
error — the "prove it broke" discipline the repo's PITFALLS.md demands.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# Make the daemon package importable (mirror tests/test_router_corpus.py).
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- defensive import: identity placeholder if R1 not yet merged (RED state) -
try:
    from daemon.text_utils import normalize_for_speech, is_speakable  # type: ignore
    _HAVE_NORMALIZE = True
except Exception:  # pragma: no cover - only during pre-fix RED
    _HAVE_NORMALIZE = False

    def normalize_for_speech(text: str) -> str:  # type: ignore
        return text or ""

    def is_speakable(text: str) -> bool:  # type: ignore
        return bool(text and text.strip())

try:
    from daemon.pipeline.process_stage import ProcessStage
    _HAVE_PROCESS = True
except Exception:  # pragma: no cover
    _HAVE_PROCESS = False
    ProcessStage = None  # type: ignore


CASES_FILE = Path(__file__).parent / "fixtures" / "spoken_corpus" / "cases.jsonl"


def _load_cases() -> list[dict]:
    cases = []
    with CASES_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


CASES = _load_cases()
CASE_IDS = [c["id"] for c in CASES]


def _pipeline_clean(text: str) -> str:
    """Run text through the REAL ProcessStage cleaner (the live audio path)."""
    stage = ProcessStage()
    return stage._clean_text_sync(text)


def _assert_contract(case: dict, rendered: str, *, check_forbid_re: bool, label: str) -> None:
    for needle in case.get("require", []):
        assert needle in rendered, (
            f"[{label}:{case['id']}] required substring {needle!r} missing.\n"
            f"  raw      = {case['raw']!r}\n  rendered = {rendered!r}\n  ({case['note']})"
        )
    for needle in case.get("forbid", []):
        assert needle not in rendered, (
            f"[{label}:{case['id']}] forbidden substring {needle!r} leaked through.\n"
            f"  raw      = {case['raw']!r}\n  rendered = {rendered!r}\n  ({case['note']})"
        )
    if check_forbid_re:
        for pat in case.get("forbid_re", []):
            assert re.search(pat, rendered) is None, (
                f"[{label}:{case['id']}] forbidden pattern {pat!r} matched.\n"
                f"  raw      = {case['raw']!r}\n  rendered = {rendered!r}\n  ({case['note']})"
            )


# ===========================================================================
# normalize_for_speech() — the pure, idempotent unit
# ===========================================================================

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_normalize_contract(case):
    if not _HAVE_NORMALIZE:
        pytest.fail("daemon.text_utils.normalize_for_speech does not exist yet (R1 not merged)")
    rendered = normalize_for_speech(case["raw"])
    _assert_contract(case, rendered, check_forbid_re=True, label="normalize")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_normalize_is_idempotent(case):
    """normalize(normalize(x)) == normalize(x) — a fixed point. Required because
    condensed/summarized content re-enters the cleaner and is re-normalized."""
    if not _HAVE_NORMALIZE:
        pytest.fail("normalize_for_speech missing")
    once = normalize_for_speech(case["raw"])
    twice = normalize_for_speech(once)
    assert once == twice, (
        f"[{case['id']}] normalize is NOT idempotent.\n"
        f"  once  = {once!r}\n  twice = {twice!r}"
    )


# ===========================================================================
# ProcessStage._clean_text_sync — the real live audio path (end-to-end)
# ===========================================================================

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_pipeline_clean_contract(case):
    if not _HAVE_PROCESS:
        pytest.skip("ProcessStage import failed")
    rendered = _pipeline_clean(case["raw"])
    # forbid_re is line-anchored; ProcessStage collapses whitespace to one line,
    # so only check literal require/forbid here (forbid_re covered by normalize test).
    _assert_contract(case, rendered, check_forbid_re=False, label="pipeline")


# ===========================================================================
# Blanket markup blacklist — nothing in this set may ever reach the voice
# ===========================================================================

_MARKUP_BLACKLIST = [
    # '**' is markup ONLY when not between two alphanumerics (2**8 is math).
    ("bold_stars", re.compile(r"(?<![A-Za-z0-9])\*\*|\*\*(?![A-Za-z0-9])")),
    ("atx_header", re.compile(r"(?m)^\s{0,3}#{1,6}\s")),
    ("backtick", re.compile(r"`")),
    ("box_drawing", re.compile(r"[─-╿▀-▟★☆]")),
    ("md_link", re.compile(r"\]\(")),
    ("html_entity", re.compile(r"&(?:gt|lt|amp|#\d+);")),
    # --- code/programmatic-syntax gibberish (R5) ---------------------------
    ("eq_run", re.compile(r"={2,}")),                       # ==, === spoken "equals equals"
    ("neq_op", re.compile(r"!==?")),                        # != , !==
    ("logic_op", re.compile(r"&&|\|\|")),                   # && , || (single | = shell pipe, kept)
    ("arrow_op", re.compile(r"=>|->")),                     # => , ->
    ("diff_hunk", re.compile(r"@@[^@\n]*@@")),
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("hex_color", re.compile(r"#[0-9a-fA-F]{6}")),
    # a hex run with BOTH a letter and a digit = a hash (deadbeef/1234567 exempt)
    ("hex_hash", re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]{7,40}\b")),
    # same lowercase letter repeated 3+ times = mangled-blob residue ("h h h")
    ("lone_letter_run", re.compile(r"(?:(?<=\s)|^)([b-hj-z])(?: \1){2,}(?=\s|$)")),
]


# ===========================================================================
# is_speakable() — the precision backstop: residual noise must be dropped,
# real (even terse) status must survive.
# ===========================================================================

_SPEAKABLE_KEEP = [
    "Build OK",
    "OK",                       # bare 2-letter status (adversarial-review fix)
    "UP",
    "3 errors",
    "2 errors",
    "23 passed, 4 failed.",
    "the router tests passed",
    "I found a race condition",
    "Deploy succeeded in production",
    # --- R5 semantic-gate recall guards (eval-set LEGIT; must NOT drop) -----
    "1 failed, 6 passed.",          # inflected verbs (pass/fail) stemmed to dict
    "16 passed.",                   # single inflected status word
    "0 errors.",
    "warning: Failed to clone files; falling back to full copy.",
    "Duration: 7m (476s)",          # number+unit version tokens are not gibberish
    "/docs/oauth2-redirect /redoc /healthz",   # tech-allowlist path words
    "schema_version: 0.1.0 Both imports OK without PYTHONPATH",
    "Wave 2 dispatched. Plan executor is running in worktree isolation",
    # ls residue AFTER real prose (perm bits not at line start) stays speakable:
    "uv 0.8.9 built then -rw-r--r-- 1 user wheel 1252 May 4",
    "ffprobe version 8.1.1 the FFmpeg developers built with clang",
    # --- TASK B guards: env/numeric-dump fixes must NOT over-drop -----------
    "we set x = 5 in the config",       # single 'A = B'-style assignment kept
    "the release on 2024-01-15 went smoothly",  # bare date (no T/Z) kept
    "order 1234567890123456 shipped overnight",  # single long id is one token
    "account 12345678901234567890 is active",
    "Duration: 7m (476s)",              # 2 numeric tokens, not a >=4 run
    "the build finished with 3 errors and 1 warning",  # numbers split by words
]
_SPEAKABLE_DROP = [
    "",
    "   ",
    "()",                                   # emptied tuple residue
    ".",                                    # lone punctuation
    "user 43965 0.0 0.1 442375712 49328 S 2 0 33",   # ps output
    "x 5 9 2 1 0 4 7",                       # number dump with one stray letter
    # --- R5 semantic-gate gibberish (eval-set GIBBERISH; must DROP) ---------
    # NOTE: these are the post-normalization forms is_speakable sees live —
    # SHAs have already been stripped, so what remains is the residue gate.
    "-rw-r--r-- 1 user wheel 1196 May 4 ErrorBanner.tsx",   # ls -l line
    "-rw-r--r--@ 1 user staff 5841 Jun 12 failure_taxonomy.py",
    "drwxr-xr-x 10 user wheel 320 May 4",                   # ls -l dir line
    "-r-xr--r--@ 1 root wheel 235648 May 11 com.panic.NovaPrivilegedHelper",
    "agent- agent- agent-",                  # agent-id dump after sha strip (zero real words)
    # --- TASK B survivor 1: env-var/timestamp assignment dump (digit-by-digit)
    # Raw form (ISO timestamp present) AND post-normalize form (timestamp gone).
    "PLAN_START_TIME=2026-05-04T22:06:50Z PLAN_START_EPOCH=1777932410",
    "PLAN_START_TIME= PLAN_START_EPOCH=1777932410",
    # --- TASK B survivor 2: ps/data dump — word header inflates the ratio, but
    # a run of >=4 consecutive numeric columns is the data-dump tell.
    "=== Daemons running === user 39608 13.8 0.1 442454016 48976 s010",
    "= Daemons running = user 39608 13.8 0.1 442454016 48976 s010",
]


@pytest.mark.parametrize("text", _SPEAKABLE_KEEP)
def test_is_speakable_keeps_real_status(text):
    assert is_speakable(text) is True, f"real status wrongly dropped: {text!r}"


@pytest.mark.parametrize("text", _SPEAKABLE_DROP)
def test_is_speakable_drops_noise(text):
    assert is_speakable(text) is False, f"non-lexical noise wrongly kept: {text!r}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_no_markup_blacklist(case):
    """Universal guard: regardless of per-case contracts, these tokens must
    never survive normalization."""
    if not _HAVE_NORMALIZE:
        pytest.fail("normalize_for_speech missing")
    rendered = normalize_for_speech(case["raw"])
    for name, pat in _MARKUP_BLACKLIST:
        assert pat.search(rendered) is None, (
            f"[{case['id']}] blacklisted markup {name!r} leaked: {rendered!r}"
        )
