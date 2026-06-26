"""Wave 1.B unit tests for ContentRouter against the captured event corpus.

Each line of ``tests/fixtures/event_corpus.jsonl`` is one test case:

    {"name": "...", "expected": {"should_speak": ..., "category": ...,
                                   "needs_summarization": ...}, "event": {...}}

The classifier is tested in isolation with a mock OllamaSummarizer (the real
one comes from W1.C). ``classify_event`` is the primary surface; ``route``
is exercised separately on a couple of representative cases to cover the
summarize-and-wrap path.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

# Add project root to sys.path so `daemon.*` imports resolve when pytest is
# invoked from any directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.content_router import ContentRouter  # noqa: E402
from daemon.tts_types import Category, RoutedItem  # noqa: E402
from daemon.providers.ollama_provider import OllamaProvider  # noqa: E402


CORPUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "event_corpus.jsonl"


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockOllamaSummarizer:
    """Minimal stand-in for the real OllamaSummarizer (W1.C).

    ``summarize`` returns a deterministic placeholder; ``last_call`` records
    arguments so tests can assert it was invoked when expected.
    """

    def __init__(self, response: str = "[mock summary]") -> None:
        self.response = response
        self.calls: list[tuple[str, Category, str]] = []

    async def summarize(self, content: str, category: Category, context_hint: str = "",
                        allow_fallback: bool = True) -> str:
        self.calls.append((content, category, context_hint))
        return self.response


def _load_corpus() -> list[dict]:
    cases = []
    with CORPUS_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


# ---------------------------------------------------------------------------
# Parametrized corpus test
# ---------------------------------------------------------------------------

CORPUS = _load_corpus()


@pytest.mark.parametrize(
    "case",
    CORPUS,
    ids=[c["name"] for c in CORPUS],
)
def test_classify_corpus(case: dict) -> None:
    """Each fixture row asserts (should_speak, category) on classify_event."""
    expected = case["expected"]
    event = case["event"]

    summarizer = MockOllamaSummarizer()
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))

    decision = asyncio.run(router.classify_event(event))

    # 1. should_speak gate
    assert decision.should_speak is bool(expected["should_speak"]), (
        f"[{case['name']}] should_speak mismatch — "
        f"got {decision.should_speak} ({decision.category.value if decision.should_speak else 'silence'}), "
        f"raw_excerpt={decision.raw_excerpt!r}"
    )

    # 2. category gate (only meaningful when should_speak=True)
    if expected["should_speak"]:
        assert decision.category.value == expected["category"], (
            f"[{case['name']}] category mismatch — "
            f"got {decision.category.value}, expected {expected['category']}"
        )

    # 3. optional needs_summarization assertion
    if "needs_summarization" in expected and expected["should_speak"]:
        assert decision.needs_summarization is bool(expected["needs_summarization"]), (
            f"[{case['name']}] needs_summarization mismatch — "
            f"got {decision.needs_summarization}, expected {expected['needs_summarization']}"
        )


# ---------------------------------------------------------------------------
# Targeted tests around the route() / summarize / RoutedItem path
# ---------------------------------------------------------------------------

def _short_final_answer_event() -> dict:
    return {
        "command": "stop_event",
        "session_id": "abc",
        "content": "Done. Bug fixed.",
        "transcript_path": "/tmp/t.jsonl",
        "stop_hook_active": False,
        "event_id": "ev-short",
        "ts": 1700000000.0,
    }


def _long_final_answer_event() -> dict:
    return {
        "command": "stop_event",
        "session_id": "abc",
        "content": (
            "I refactored the cache layer end-to-end so reads go through the "
            "write-through path, instrumented per-prefix counters, and confirmed "
            "the eviction rate halved in the rebench. The on-disk format is "
            "unchanged so warmup behaves identically."
        ),
        "transcript_path": "/tmp/t.jsonl",
        "stop_hook_active": False,
        "event_id": "ev-long",
        "ts": 1700000001.0,
    }


def test_route_short_returns_routed_item_no_summary() -> None:
    summarizer = MockOllamaSummarizer()
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))

    item = asyncio.run(router.route(_short_final_answer_event()))

    assert isinstance(item, RoutedItem)
    assert item.session_id == "abc"
    assert item.decision.should_speak is True
    assert item.decision.category == Category.FINAL_ANSWER
    assert item.decision.needs_summarization is False
    # No summarization should have happened: short input.
    assert summarizer.calls == []
    # Content is verbatim.
    assert "Done." in item.decision.content


def test_route_long_calls_summarizer() -> None:
    summarizer = MockOllamaSummarizer(response="Refactored cache; halved evictions.")
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))

    item = asyncio.run(router.route(_long_final_answer_event()))

    assert isinstance(item, RoutedItem)
    assert item.decision.should_speak is True
    assert item.decision.needs_summarization is False  # cleared post-summary
    assert item.decision.content == "Refactored cache; halved evictions."
    assert len(summarizer.calls) == 1
    called_content, called_category, called_hint = summarizer.calls[0]
    assert called_category == Category.FINAL_ANSWER
    assert "cache" in called_content.lower()


def test_route_silence_returns_none() -> None:
    summarizer = MockOllamaSummarizer()
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))

    silent_event = {
        "command": "tool_event",
        "phase": "post",
        "event_id": "silent",
        "ts": 1700000000.0,
        "session_id": "abc",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
        "tool_use_id": "u",
        "duration_ms": 4,
        "tool_response": {
            "stdout": "1\thello\n",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
    }
    assert asyncio.run(router.route(silent_event)) is None
    assert summarizer.calls == []


# ---------------------------------------------------------------------------
# Robustness — never raise on garbage input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "garbage",
    [
        None,
        "not a dict",
        42,
        [],
        {},
        {"command": "tool_event"},  # no tool_response
        {"command": "tool_event", "tool_response": "not a dict"},
        {"command": "stop_event"},  # no content
    ],
    ids=[
        "none", "string", "int", "list", "empty_dict",
        "tool_event_no_response", "tool_event_bad_response", "stop_no_content",
    ],
)
def test_classify_never_raises(garbage) -> None:
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(garbage))
    assert decision.should_speak is False
    # raw_excerpt should hold a diagnostic.
    assert decision.raw_excerpt != ""


# ---------------------------------------------------------------------------
# Dedupe — same content classified twice → second is silence
# ---------------------------------------------------------------------------

def test_dedupe_drops_repeated_content() -> None:
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    ev = _short_final_answer_event()

    first = asyncio.run(router.classify_event(ev))
    assert first.should_speak is True

    # Same content again → drop.
    ev2 = dict(ev)
    ev2["event_id"] = "ev-short-2"
    second = asyncio.run(router.classify_event(ev2))
    assert second.should_speak is False
    assert "duplicate" in second.raw_excerpt.lower()


# ---------------------------------------------------------------------------
# Drop filter — code-block / boilerplate / system-reminder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,expect_speak",
    [
        ("```python\nx=1\n```", False),
        ("Here is the file you asked about.", False),
        ("<system-reminder>internal stuff</system-reminder>", False),
        ("src/foo/bar.py", False),
        ("Done. Bug fixed in the auth module.", True),
        # RECALL FIX 2026-06-19: a long answer that merely OPENS with a
        # boilerplate word is a real finding, not filler — it must speak.
        # (shadow.log replay found 6 such answers, 872–4524 chars, wrongly
        # vetoed as "boilerplate prefix".)
        (
            "I'll trace the bug. The Settings overlay still captures clicks even "
            "when the coach renders above it; closing it before opening the next "
            "step fixes the menu staying open. Fixed in the overlay handler.",
            True,
        ),
    ],
    ids=["code_fence", "boilerplate", "system_reminder", "path_only", "ok",
         "boilerplate_with_body"],
)
def test_stop_drop_filter(content: str, expect_speak: bool) -> None:
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    ev = {
        "command": "stop_event",
        "session_id": "x",
        "content": content,
        "transcript_path": "/tmp/t.jsonl",
        "stop_hook_active": False,
        "event_id": "drop-1",
        "ts": 1700000000.0,
    }
    decision = asyncio.run(router.classify_event(ev))
    assert decision.should_speak is expect_speak


# ---------------------------------------------------------------------------
# Set queue manager late-bind smoke test
# ---------------------------------------------------------------------------

class _StubQM:
    def get_pressure(self, session_id: str) -> float:
        return 1.0


def test_set_queue_manager_late_bind() -> None:
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    assert router.queue_manager is None
    router.set_queue_manager(_StubQM())
    assert router.queue_manager is not None


# ---------------------------------------------------------------------------
# W3.B — pressure backpressure tests
#
# Under high lag (RED/BLACK tier), ContentRouter must silence borderline
# content via the priority-gate. ERROR always passes regardless of pressure.
# Cutoff = pressure * 2:
#   GREEN  (1.0) → cutoff 2  — nothing dropped
#   YELLOW (1.5) → cutoff 3  — nothing dropped (LOW=3 still passes)
#   RED    (2.5) → cutoff 5  — drops PRIORITY_LOW (3); NORMAL(5)+ passes
#   BLACK  (5.0) → cutoff 10 — drops everything except ERROR (10)
# ---------------------------------------------------------------------------


class _PressureQM:
    """Configurable QueueManager stub returning a fixed pressure value."""
    def __init__(self, pressure: float) -> None:
        self._pressure = pressure

    def get_pressure(self, session_id: str) -> float:
        return self._pressure


def _bash_lowpri_event() -> dict:
    """Build a Bash post event whose only signal is digits-in-output —
    this routes through the binary-LLM ambiguous-middle branch with
    priority=PRIORITY_LOW (the borderline tier).

    The structured-extractor's bash branch checks the LAST 3 LINES for
    digits/domain-keywords; we put the digit-bearing line earlier and end
    with bare prose so the tail-extractor returns empty. The ambiguous
    middle re-checks the full stdout for digit/keyword and triggers the
    binary LLM judge (which mocks return 'SPEAK').
    """
    return {
        "command": "tool_event",
        "phase": "post",
        "event_id": "lowpri-1",
        "ts": 1.0,
        "session_id": "p-test",
        "tool_name": "Bash",
        "tool_input": {"command": "echo done"},
        "tool_response": {
            # First line has digits to trigger ambiguous middle full-stdout
            # check; the last 3 lines are bare prose so structured-extractor
            # tail returns empty and we fall to LOW-priority binary judge.
            "stdout": (
                "deploy version 12345 from yesterday\n"
                "everything seems quiet now and gentle\n"
                "the wind blows softly through pines\n"
                "smooth quiet morning all around here yes"
            ),
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
        "tool_use_id": "u-low",
        "duration_ms": 200,
        "transcript_path": "",
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PostToolUse",
    }


def _bash_test_event() -> dict:
    """Bash event matching the test-result extractor → STATUS, PRIORITY_NORMAL."""
    ev = _bash_lowpri_event()
    ev["event_id"] = "norm-1"
    ev["tool_input"]["command"] = "pytest tests/"
    ev["tool_response"]["stdout"] = "===== 23 passed, 4 failed in 12.3s ====="
    return ev


def _stop_final_answer_event() -> dict:
    """Stop event → FINAL_ANSWER, PRIORITY_HIGH."""
    return {
        "command": "stop_event",
        "session_id": "p-test",
        "content": "Done. Bug is fixed.",
        "transcript_path": "/tmp/t.jsonl",
        "stop_hook_active": False,
        "event_id": "fa-1",
        "ts": 1700000000.0,
    }


def _bash_error_event() -> dict:
    """Bash event with stderr → ERROR, PRIORITY_ERROR."""
    ev = _bash_lowpri_event()
    ev["event_id"] = "err-1"
    ev["tool_response"]["stdout"] = ""
    ev["tool_response"]["stderr"] = "error: cargo: command not found in path"
    return ev


def _make_speak_summarizer() -> MockOllamaSummarizer:
    """For binary-LLM ambiguous-Bash path: respond 'SPEAK' so the low-pri
    Bash gets a should_speak=True decision (priority=LOW). This is needed
    so the priority-gate has something to silence under pressure.
    """
    return MockOllamaSummarizer(response="SPEAK")


def test_pressure_green_unchanged_baseline() -> None:
    """pressure=1.0 (GREEN) — behavior unchanged from baseline."""
    router = ContentRouter(config={}, provider=OllamaProvider(_make_speak_summarizer()))
    router.set_queue_manager(_PressureQM(1.0))

    # Low-pri Bash (LOW=3) — speaks at GREEN
    item_low = asyncio.run(router.route(_bash_lowpri_event()))
    assert item_low is not None, "GREEN must not silence LOW-priority items"
    assert item_low.decision.priority == 3  # PRIORITY_LOW

    # Normal-pri test result (NORMAL=5) — speaks at GREEN
    router2 = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router2.set_queue_manager(_PressureQM(1.0))
    item_norm = asyncio.run(router2.route(_bash_test_event()))
    assert item_norm is not None
    assert item_norm.decision.category == Category.STATUS

    # Final answer (HIGH=7) — speaks at GREEN
    router3 = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router3.set_queue_manager(_PressureQM(1.0))
    item_fa = asyncio.run(router3.route(_stop_final_answer_event()))
    assert item_fa is not None
    assert item_fa.decision.category == Category.FINAL_ANSWER


def test_pressure_red_silences_borderline_content() -> None:
    """pressure=2.5 (RED) — borderline (PRIORITY_LOW) silenced, NORMAL/HIGH pass."""
    # PRIORITY_LOW (3) — silenced under RED (cutoff = 5)
    router_low = ContentRouter(config={}, provider=OllamaProvider(_make_speak_summarizer()))
    router_low.set_queue_manager(_PressureQM(2.5))
    assert asyncio.run(router_low.route(_bash_lowpri_event())) is None, (
        "RED pressure must silence PRIORITY_LOW (borderline) content"
    )

    # PRIORITY_NORMAL (5) — survives RED (5 >= 5)
    router_norm = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router_norm.set_queue_manager(_PressureQM(2.5))
    item_norm = asyncio.run(router_norm.route(_bash_test_event()))
    assert item_norm is not None, "RED must not silence NORMAL-priority STATUS"
    assert item_norm.decision.category == Category.STATUS

    # PRIORITY_HIGH (7) — survives RED
    router_hi = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router_hi.set_queue_manager(_PressureQM(2.5))
    item_fa = asyncio.run(router_hi.route(_stop_final_answer_event()))
    assert item_fa is not None
    assert item_fa.decision.category == Category.FINAL_ANSWER


def test_pressure_black_only_errors_speak() -> None:
    """pressure=5.0 (BLACK) — only ERROR-class items speak."""
    # PRIORITY_LOW (3) — silenced
    router_low = ContentRouter(config={}, provider=OllamaProvider(_make_speak_summarizer()))
    router_low.set_queue_manager(_PressureQM(5.0))
    assert asyncio.run(router_low.route(_bash_lowpri_event())) is None

    # PRIORITY_NORMAL (5) — silenced (5 < 10)
    router_norm = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router_norm.set_queue_manager(_PressureQM(5.0))
    assert asyncio.run(router_norm.route(_bash_test_event())) is None, (
        "BLACK must silence STATUS items"
    )

    # PRIORITY_HIGH (7) — silenced (7 < 10)
    router_hi = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router_hi.set_queue_manager(_PressureQM(5.0))
    assert asyncio.run(router_hi.route(_stop_final_answer_event())) is None, (
        "BLACK must silence even FINAL_ANSWER (only ERROR speaks)"
    )

    # PRIORITY_ERROR (10) — passes
    router_err = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    router_err.set_queue_manager(_PressureQM(5.0))
    item_err = asyncio.run(router_err.route(_bash_error_event()))
    assert item_err is not None, "BLACK must NEVER silence ERROR"
    assert item_err.decision.category == Category.ERROR


def test_pressure_error_always_passes_regardless() -> None:
    """ERROR always passes regardless of pressure (every tier)."""
    for pressure in (1.0, 1.5, 2.5, 5.0, 100.0):
        router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
        router.set_queue_manager(_PressureQM(pressure))
        item = asyncio.run(router.route(_bash_error_event()))
        assert item is not None, (
            f"ERROR must NEVER be silenced (pressure={pressure})"
        )
        assert item.decision.category == Category.ERROR


def test_pressure_no_qm_bound_treats_as_green() -> None:
    """Without a QueueManager bound, pressure defaults to 1.0 (GREEN behavior)."""
    router = ContentRouter(config={}, provider=OllamaProvider(_make_speak_summarizer()))
    # No queue_manager set
    assert router.queue_manager is None
    item = asyncio.run(router.route(_bash_lowpri_event()))
    assert item is not None, "Unbound QM should not silence anything"


# ---------------------------------------------------------------------------
# Wave 2.5 tuning: regression tests for false positives observed in shadow.log
# ---------------------------------------------------------------------------

def _bash_event(stdout: str, stderr: str = "") -> dict:
    """Helper: build a minimal post-tool-use Bash event with given output."""
    return {
        "command": "tool_event",
        "phase": "post",
        "event_id": "test-evt",
        "ts": 1.0,
        "session_id": "test-session",
        "tool_name": "Bash",
        "tool_input": {"command": "test"},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
        "tool_use_id": "test-1",
        "duration_ms": 10,
        "transcript_path": "",
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PostToolUse",
    }


@pytest.mark.parametrize("stdout,reason", [
    # Real shadow.log false positive: ls -la output classified as STATUS
    (
        "srw-rw-rw-@ 1 user  wheel  0 May  4 13:31 /tmp/tts_daemon.sock",
        "ls -la single line",
    ),
    # ls -la with multiple files
    (
        "drwxr-xr-x  3 user  staff   96 May  4 12:00 daemon\n"
        "-rw-r--r--  1 user  staff  148 May  4 13:00 types.py\n"
        "lrwxr-xr-x  1 user  staff   12 May  4 12:30 link -> target",
        "ls -la multi-line",
    ),
    # URL/path-list dump
    (
        "/docs/oauth2-redirect /redoc /healthz",
        "URL/path list dump",
    ),
])
def test_status_false_positive_filelistings_silenced(stdout: str, reason: str) -> None:
    """File listings and path-list dumps must not be spoken as STATUS."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(_bash_event(stdout)))
    assert decision.should_speak is False, (
        f"shadow false positive ({reason}) leaked through: {decision.content!r}"
    )


@pytest.mark.parametrize("stderr,reason", [
    # Real shadow.log false positive: cargo warning classified as ERROR
    (
        "warning: Failed to clone files; falling back to full copy. "
        "This may lead to degraded performance.",
        "cargo warning with 'Failed' keyword",
    ),
    (
        "warn: deprecated API used; please upgrade",
        "warn: prefix",
    ),
    (
        "deprecated: this function will be removed in v2.0",
        "deprecated: prefix",
    ),
])
def test_error_false_positive_warnings_silenced(stderr: str, reason: str) -> None:
    """Warning/deprecated lines must not be classified as ERROR."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(_bash_event("", stderr=stderr)))
    assert decision.category != Category.ERROR, (
        f"shadow false positive ({reason}) flagged ERROR: {decision.content!r}"
    )


@pytest.mark.parametrize("stdout,reason", [
    # Real shadow.log false positive: git commit log line with "failing tests"
    (
        "6a85473 test(01-02): add failing tests for license_check + model_license_scan (...)",
        "git log line with 'failing'",
    ),
    # Real shadow.log false positive: worktree commit output
    (
        "[worktree-agent-a91a55a192f7384ec 1d78dc3] test(01-02): add failing test for verify-license",
        "git worktree commit output",
    ),
    # Bare commit hash + log message
    (
        "deadbeef fix: resolve flaky test failure in auth module",
        "git short-hash + 'failure' keyword",
    ),
])
def test_error_false_positive_git_commits_silenced(stdout: str, reason: str) -> None:
    """Git commit log lines (hex prefix or [branch hash] prefix) must not
    be classified as ERROR even when they contain failing/error keywords."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(_bash_event(stdout)))
    assert decision.category != Category.ERROR, (
        f"shadow false positive ({reason}) flagged ERROR: {decision.content!r}"
    )


def test_real_error_still_fires_after_tuning() -> None:
    """Sanity: tuning didn't disable legitimate ERROR detection."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(
        _bash_event("", stderr="error: cargo: command not found")
    ))
    assert decision.should_speak is True
    assert decision.category == Category.ERROR


def test_real_test_results_still_fire_after_tuning() -> None:
    """Sanity: tuning didn't disable legitimate STATUS detection."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    decision = asyncio.run(router.classify_event(
        _bash_event("===== 23 passed, 4 failed in 12.3s =====")
    ))
    assert decision.should_speak is True
    assert decision.category == Category.STATUS


# ---------------------------------------------------------------------------
# Wave 2.5 enrichment: context_hint must include the bash target so model's
# spoken summary can include WHAT was being run, not just bare counts.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected_target", [
    ("pytest tests/test_router.py -v", "router"),
    ("pytest tests/fixtures/test_corpus.py", "corpus"),
    ("pytest -k test_classify", "the tests"),  # flag-only run still names the object (the tests)
    ("npm run build:prod", "build:prod"),
    ("cargo test queue::manager", "queue::manager"),
    ("cargo build --release", "cargo build"),
    ("cargo run", "cargo run"),
    ("make install", "install"),
    ("make test/integration", "test/integration"),
    ("go test ./internal/...", "./internal/..."),
    ("python3 scripts/analyze.py --since 2026", "analyze"),
    ("ls -la /tmp", ""),  # not a test/build runner; no target hint
])
def test_bash_target_hint_extraction(command: str, expected_target: str) -> None:
    """Verify _bash_target_hint pulls out the right command target."""
    assert ContentRouter._bash_target_hint(command) == expected_target


def test_bash_post_includes_target_in_context_hint() -> None:
    """When _extract_bash receives both stdout and the originating command,
    the returned context_hint must include the parsed target so the summarizer
    can speak 'twenty-three passed in test_router' instead of bare counts."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = _bash_event("23 passed, 4 failed in 12.3s")
    event["tool_input"]["command"] = "pytest tests/test_router.py -v"
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is True
    assert decision.category == Category.STATUS
    # context_hint should mention the target
    assert "router" in decision.context_hint.lower(), (
        f"target missing from context_hint: {decision.context_hint!r}"
    )


def test_bash_post_no_target_falls_back_to_generic_hint() -> None:
    """If the command lacks a recognizable target, context_hint stays generic
    (no garbage in)."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = _bash_event("23 passed, 4 failed")
    event["tool_input"]["command"] = "./run_tests.sh"  # no recognized runner
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is True
    assert decision.context_hint == "test result"  # bare, no "from X"


# ---------------------------------------------------------------------------
# R5 object/context: grep names the FILE searched when the pattern is missing,
# so the listener hears "grep in router.py" instead of bare "grep result".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_input,expected", [
    ({"path": "daemon/content_router.py"}, "content_router.py"),
    ({"glob": "router.py"}, "router.py"),
    ({"file": "/abs/path/to/queue_manager.py"}, "queue_manager.py"),
    ({"path": "."}, ""),            # cwd is not a salient target
    ({"path": "src"}, ""),          # broad directory is not a useful WHAT
    ({"glob": "**/*.py"}, ""),      # broad glob is not a useful WHAT
    ({}, ""),                       # nothing to name
])
def test_grep_target_file_extraction(tool_input: dict, expected: str) -> None:
    assert ContentRouter._grep_target_file(tool_input) == expected


def test_extract_grep_names_file_when_pattern_missing() -> None:
    """When grep has no pattern but does scope a file, the context_hint must
    name that file ('grep in X') rather than the vague 'grep result' that forced
    model to synthesize 'the search'."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = {
        "tool_name": "Grep",
        "tool_input": {"path": "daemon/content_router.py"},  # no pattern
    }
    content, hint = router._extract_grep(event, "match line one\nmatch line two\n")
    assert content == "2 matches."
    assert hint == "grep in content_router.py", hint


def test_extract_grep_prefers_pattern_over_file() -> None:
    """A present pattern still wins — it names WHAT was searched for."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "TODO", "path": "router.py"},
    }
    _content, hint = router._extract_grep(event, "a\nb\nc\n")
    assert hint == "grep for TODO", hint


# ---------------------------------------------------------------------------
# Wave 2.5 enrichment: per-output verbosity classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,target,expected", [
    # Generic short output WITH target → wants targeting
    ("23 passed, 4 failed.", "router", "targeted"),
    ("47 matches.", "core_module", "targeted"),
    ("Done.", "build", "targeted"),
    # Long content → terse (self-contained)
    (
        "ImportError: cannot import name 'foo' from 'bar' "
        "(some module path) — Python 3.13 expects a different signature.",
        "test_module",
        "terse",
    ),
    # Specific file ref → terse (already self-evident)
    ("Failed in auth.py at line 42.", "test_router", "terse"),
    ("queue::manager test failed.", "queue", "terse"),
    # Already mentions the target → terse (no double-naming)
    ("23 passed in router.", "router", "terse"),
    # No target available → terse (nothing to prepend)
    ("23 passed, 4 failed.", "", "terse"),
    # Line-number ref → terse
    ("Compile error at line 142.", "compile", "terse"),
])
def test_verbosity_for_per_output(content: str, target: str, expected: str) -> None:
    """The verbosity classifier should pick TARGETED only when the content
    is short, generic, and the target isn't already woven in."""
    assert ContentRouter._verbosity_for(content, target) == expected


def test_targeted_post_prefixes_target_naturally() -> None:
    """End-to-end: short generic test result + recognizable command should
    speak 'In test_router: 23 passed, 4 failed.' rather than bare counts."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = _bash_event("23 passed, 4 failed.")
    event["tool_input"]["command"] = "pytest tests/test_router.py"
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is True
    assert decision.content.lower().startswith("in router:"), (
        f"expected target-prefixed content; got: {decision.content!r}"
    )


def test_terse_post_skips_prefix_when_self_evident() -> None:
    """End-to-end: long content already specific shouldn't get prefixed."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    long_specific = (
        "ImportError: cannot import name 'foo' from auth.py at line 42. "
        "Module path mismatches expected layout."
    )
    event = _bash_event(long_specific, stderr=long_specific)
    event["tool_input"]["command"] = "pytest tests/test_router.py"
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is True
    # Content is long + has filename/line ref — no target prefix
    assert not decision.content.lower().startswith("in router:"), (
        f"unexpected target prefix on self-evident content: {decision.content!r}"
    )


# ---------------------------------------------------------------------------
# R-context enrichment: the binary SPEAK/SKIP judge now sees a COMPACT
# session/project context phrase (command target + project dir + session-local
# recency) so it judges relevance in context, not in a vacuum. Ollama is not
# available to test agents, so we verify PROMPT CONSTRUCTION deterministically:
#   (a) _judge_context() is a pure helper returning the expected phrase.
#   (b) the assembled context reaches the summarizer via _binary_llm_judge.
# These tests must NOT loosen any gate — only enrich the judge's input.
# ---------------------------------------------------------------------------

def _judge_event(command: str, cwd: str = "/Users/dev/myproj") -> dict:
    """A Bash post event whose stdout routes through the ambiguous-middle
    binary-judge branch (digits + domain keyword, no structured extractor)."""
    ev = _bash_lowpri_event()
    ev["tool_input"]["command"] = command
    ev["cwd"] = cwd
    return ev


def test_judge_context_names_command_target_and_project() -> None:
    """Pure helper: target (from the command) + project dir (basename of cwd)
    are woven into a compact phrase."""
    event = _judge_event("pytest tests/test_router.py -v", cwd="/Users/dev/myproj")
    ctx = ContentRouter._judge_context(event, recently_spoken=False)
    assert "ran router" in ctx, ctx
    assert "in myproj" in ctx, ctx
    # Compact: a couple of clauses, not a transcript.
    assert len(ctx) <= 160, ctx
    assert "already spoken" not in ctx


def test_judge_context_reflects_recently_spoken() -> None:
    """The session-local recency flag surfaces as an explicit clause so the
    judge can downweight repeated output."""
    event = _judge_event("npm run build:prod")
    ctx = ContentRouter._judge_context(event, recently_spoken=True)
    assert "ran build:prod" in ctx, ctx
    assert "already spoken this session" in ctx, ctx


def test_judge_context_skips_nonsalient_cwd_and_empty_command() -> None:
    """No command target and a non-salient cwd ('/tmp') → empty context, so the
    judge falls back to the bare tool-name prompt (no garbage in)."""
    event = {"command": "tool_event", "tool_name": "Bash",
             "tool_input": {"command": "ls -la /tmp"}, "cwd": "/tmp"}
    assert ContentRouter._judge_context(event, recently_spoken=False) == ""


def test_judge_context_handles_missing_fields() -> None:
    """Robust to malformed / partial events (no tool_input, no cwd)."""
    assert ContentRouter._judge_context({}, recently_spoken=False) == ""
    assert ContentRouter._judge_context(
        {"tool_input": None, "cwd": None}, recently_spoken=False
    ) == ""


def test_binary_judge_prompt_includes_context_phrase() -> None:
    """End-to-end through the ambiguous branch: the context phrase built from
    the event reaches the summarizer as part of the BINARY_JUDGMENT prompt."""
    summarizer = MockOllamaSummarizer(response="SPEAK")
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))
    event = _judge_event("pytest tests/test_router.py", cwd="/Users/dev/myproj")
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is True
    # The judge call is the (only) summarizer call on this branch.
    assert summarizer.calls, "judge never invoked the summarizer"
    _content, category, hint = summarizer.calls[0]
    assert category == Category.STATUS
    assert hint.startswith("BINARY_JUDGMENT:"), hint
    assert "Context:" in hint, hint
    assert "ran router" in hint, hint
    assert "in myproj" in hint, hint


def test_binary_judge_recency_reflected_on_second_identical_output() -> None:
    """First identical ambiguous output is FRESH; the second (same stdout, same
    session) is flagged 'already spoken this session' in the judge prompt — the
    session-local recency signal, sourced from the per-session spoken window."""
    summarizer = MockOllamaSummarizer(response="SPEAK")
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))
    event = _judge_event("npm run build:prod", cwd="/Users/dev/myproj")

    asyncio.run(router.classify_event(event))
    first_hint = summarizer.calls[0][2]
    assert "already spoken this session" not in first_hint, first_hint

    asyncio.run(router.classify_event(event))
    second_hint = summarizer.calls[1][2]
    assert "already spoken this session" in second_hint, second_hint


def test_binary_judge_recency_is_session_scoped() -> None:
    """Recency is per-session: identical output in a DIFFERENT session is still
    FRESH (no cross-session bleed)."""
    summarizer = MockOllamaSummarizer(response="SPEAK")
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))
    ev_a = _judge_event("npm run build:prod")
    ev_a["session_id"] = "sess-A"
    ev_b = _judge_event("npm run build:prod")
    ev_b["session_id"] = "sess-B"

    asyncio.run(router.classify_event(ev_a))
    asyncio.run(router.classify_event(ev_b))
    second_hint = summarizer.calls[1][2]
    assert "already spoken this session" not in second_hint, second_hint


def test_enriched_judge_does_not_bypass_gates() -> None:
    """Precision guard: the enrichment must NOT make non-ambiguous content reach
    the judge. Output with no digit/keyword combo still gets silenced — the
    judge (summarizer) is never called."""
    summarizer = MockOllamaSummarizer(response="SPEAK")
    router = ContentRouter(config={}, provider=OllamaProvider(summarizer))
    event = _judge_event("echo hi")
    # Bare prose, no digits/domain keyword → fails the ambiguous-branch guard.
    event["tool_response"]["stdout"] = (
        "the quiet morning passes gently over the soft hills here today now"
    )
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is False
    assert summarizer.calls == [], "judge fired on non-ambiguous content"


# ---------------------------------------------------------------------------
# Harness-noise strip + consecutive-tail guard (cwd-reset spam)
# ---------------------------------------------------------------------------

from daemon.content_router import _strip_harness_noise, _tail_hash  # noqa: E402


def test_strip_harness_noise_removes_cwd_reset_line():
    # Standalone boilerplate → emptied.
    assert _strip_harness_noise(
        "Shell cwd was reset to /home/user/project"
    ).strip() == ""
    # Riding on real output → only the boilerplate line goes.
    out = _strip_harness_noise("real result line\nShell cwd was reset to /a/b/c")
    assert "Shell cwd was reset" not in out
    assert "real result line" in out
    # Fast path: no marker → identical object, no regex cost.
    s = "no marker here, just output"
    assert _strip_harness_noise(s) is s


def test_classify_tool_silences_pure_cwd_reset():
    """A Bash result whose stdout is ONLY the harness cwd-reset line must be
    silenced — it was being spoken (and repeated) before the strip."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    event = {
        "phase": "post",
        "tool_name": "Bash",
        "session_id": "s1",
        "tool_input": {"command": "cd /tmp && echo hi"},
        "tool_response": {
            "stdout": "Shell cwd was reset to /home/user/project"
        },
    }
    decision = asyncio.run(router.classify_event(event))
    assert decision.should_speak is False


def test_consecutive_tail_guard_caps_at_two():
    """Three utterances with differing preamble but an IDENTICAL last line: the
    whole-content hash differs each time (so global dedupe misses them), but the
    consecutive-tail guard must drop the 3rd+, and a different line resets it."""
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    a = "alpha details one\nstable trailing line"
    b = "beta details two\nstable trailing line"
    c = "gamma details three\nstable trailing line"

    assert _tail_hash(a) == _tail_hash(b) == _tail_hash(c)  # same tail
    assert _hash_content_differs(a, b, c)                   # but full content differs

    # 1st and 2nd: spoken (≤2 allowed in a row).
    assert router._drop_check(a) is None
    router._note_hash(a, "s")
    assert router._drop_check(b) is None
    router._note_hash(b, "s")
    # 3rd consecutive same-tail: dropped.
    assert router._drop_check(c) == "repeated trailing line >2x consecutively"

    # A different last line in between resets the run.
    d = "delta\ndifferent trailing line"
    assert router._drop_check(d) is None
    router._note_hash(d, "s")
    # Now the stable-tail line may be spoken again (run was broken). Use a fresh
    # preamble so the global hash-dedupe doesn't fire on already-noted content.
    e = "epsilon details four\nstable trailing line"
    assert router._drop_check(e) is None


def _hash_content_differs(*texts) -> bool:
    from daemon.content_router import _hash_content
    hashes = {_hash_content(t) for t in texts}
    return len(hashes) == len(texts)
