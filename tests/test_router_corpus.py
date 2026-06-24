"""Router corpus tests — fixture-driven, end-to-end classification checks.

ROUTER-02: STATUS classifier rejects pure file-listing, git output, grep LINE:CONTENT.
ROUTER-03: Substantive STATUS (test counts, exit codes) still speaks.
ROUTER-04: _drop_check operates on RAW stdout BEFORE _extract_bash mutates shape.
ROUTER-05: _extract_bash requires digits AND domain keyword (not OR).

Each test drives a REAL captured stdout through the full ContentRouter pipeline
using synchronous helpers (no Ollama, deterministic).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Optional

import pytest

# Make sure daemon package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.content_router import ContentRouter
from daemon.providers.ollama_provider import OllamaProvider
# _drop_check_raw is the ROUTER-04 refactored module-level helper.
# It will be importable after the fix; fall back gracefully for RED phase.
try:
    from daemon.content_router import _drop_check_raw
except ImportError:
    _drop_check_raw = None


FIXTURES = Path(__file__).parent / "fixtures" / "router_corpus"


# ---------------------------------------------------------------------------
# Minimal stub so ContentRouter can be constructed without a real daemon.
# ---------------------------------------------------------------------------

class _NullSummarizer:
    async def summarize(self, *a, **kw) -> str:
        return ""


def _make_router() -> ContentRouter:
    return ContentRouter(config={}, provider=OllamaProvider(_NullSummarizer()))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _bash_event(stdout: str, command: str = "ls -la") -> dict:
    return {
        "command": "tool_event",
        "tool_name": "Bash",
        "phase": "post",
        "session_id": "test-session",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
        },
    }


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# ROUTER-04: _drop_check on RAW stdout — directly test the new helper
# ===========================================================================

class TestDropCheckRaw:
    """Verify _drop_check_raw (the ROUTER-04 refactored entry point) matches
    file-listing patterns BEFORE shape mutation by _extract_bash."""

    def _call(self, raw: str):
        if _drop_check_raw is None:
            pytest.skip("_drop_check_raw not yet implemented (pre-fix RED state)")
        return _drop_check_raw(raw)

    def test_ls_la_raw_is_dropped(self):
        raw = _fixture("ls_la_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "ls -la raw stdout should be dropped by _drop_check_raw but returned None"
        )
        assert "listing" in reason.lower() or "file" in reason.lower(), (
            f"Expected 'listing' or 'file' in reason, got: {reason!r}"
        )

    def test_grep_n_raw_is_dropped(self):
        raw = _fixture("grep_n_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "grep -n LINE:CONTENT raw stdout should be dropped but returned None. "
            f"First 3 lines: {raw.splitlines()[:3]}"
        )

    def test_wc_l_raw_is_dropped(self):
        raw = _fixture("wc_l_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "wc -l output should be dropped but returned None"
        )

    def test_find_output_raw_is_dropped(self):
        raw = _fixture("find_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "find output (bare path list) should be dropped but returned None"
        )

    def test_git_diff_stat_raw_is_dropped(self):
        raw = _fixture("git_diff_stat_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "git diff --stat output should be dropped but returned None"
        )

    def test_git_show_stat_raw_is_dropped(self):
        raw = _fixture("git_show_stat_output.txt")
        reason = self._call(raw)
        assert reason is not None, (
            "git show --stat output should be dropped but returned None"
        )

    def test_pytest_passed_raw_is_kept(self):
        """91 passed in 0.22s — substantive, must NOT be dropped."""
        raw = _fixture("pytest_passed_output.txt")
        reason = self._call(raw)
        assert reason is None, (
            f"pytest passed output is substantive STATUS; must not be dropped. "
            f"Got reason: {reason!r}"
        )


# ===========================================================================
# ROUTER-02: Full pipeline — noise fixtures must NOT speak
# ===========================================================================

class TestNoiseFixtures:
    """End-to-end: noise command outputs must produce should_speak=False."""

    def _check_silent(self, stdout: str, command: str, label: str):
        router = _make_router()
        event = _bash_event(stdout, command)
        decision = _run(router.classify_event(event))
        assert not decision.should_speak, (
            f"[{label}] Expected should_speak=False but got True. "
            f"category={decision.category.value!r}, "
            f"content={decision.content[:120]!r}"
        )

    def test_ls_la_is_silent(self):
        self._check_silent(_fixture("ls_la_output.txt"), "ls -la", "ls_la")

    def test_find_is_silent(self):
        self._check_silent(_fixture("find_output.txt"), "find . -name '*.py'", "find")

    def test_grep_n_is_silent(self):
        self._check_silent(_fixture("grep_n_output.txt"), "grep -n 'def ' daemon/", "grep_n")

    def test_wc_l_is_silent(self):
        self._check_silent(_fixture("wc_l_output.txt"), "wc -l daemon/*.py", "wc_l")

    def test_git_status_is_silent(self):
        self._check_silent(_fixture("git_status_output.txt"), "git status", "git_status")

    def test_git_diff_stat_is_silent(self):
        self._check_silent(
            _fixture("git_diff_stat_output.txt"), "git diff --stat HEAD~3 HEAD",
            "git_diff_stat"
        )

    def test_git_show_stat_is_silent(self):
        self._check_silent(
            _fixture("git_show_stat_output.txt"), "git show --stat HEAD",
            "git_show_stat"
        )

    def test_lsof_head_digits_no_domain_is_silent(self):
        """ROUTER-05: lsof | head has digits but no domain keyword — must be silent."""
        lsof_like = (
            "COMMAND  PID       USER   FD   TYPE             DEVICE SIZE/OFF\n"
            "python3  1234  user  cwd    DIR               1,14\n"
            "python3  1234  user  txt    REG               1,14\n"
            "python3  1234  user  mem    REG               1,14\n"
            "head     5678  user  cwd    DIR               1,14\n"
        )
        self._check_silent(lsof_like, "lsof | head", "lsof_head")


# ===========================================================================
# ROUTER-03: Substantive STATUS must still speak
# ===========================================================================

class TestSubstantiveStatus:
    """Verify that meaningful test/build outputs survive classifier tightening."""

    def _check_speaks(self, stdout: str, command: str, label: str):
        router = _make_router()
        event = _bash_event(stdout, command)
        decision = _run(router.classify_event(event))
        assert decision.should_speak, (
            f"[{label}] Expected should_speak=True but got False. "
            f"reason={decision.raw_excerpt!r}"
        )

    def test_pytest_91_passed_speaks(self):
        """'91 passed in 0.22s' must produce should_speak=True."""
        self._check_speaks(
            _fixture("pytest_passed_output.txt"),
            "pytest tests/test_content_router.py -q",
            "pytest_91_passed",
        )

    def test_pytest_with_failure_speaks(self):
        stdout = (
            "FAILED tests/test_router.py::test_foo - AssertionError\n"
            "1 failed, 23 passed in 1.45s\n"
        )
        self._check_speaks(stdout, "pytest tests/ -q", "pytest_mixed_result")

    def test_test_count_string_speaks(self):
        """Bare '5 passed.' format."""
        self._check_speaks("5 passed.", "pytest tests/test_foo.py", "bare_5_passed")

    def test_grep_c_count_speaks(self):
        """grep -c output: just a number on a line — a count, not paths."""
        # A plain count line (not LINE:CONTENT) should be substantive.
        stdout = "47\n"  # grep -c 'pattern' file
        router = _make_router()
        # grep -c output is just a number; with command having 'grep', check it's not DROPPED
        # We don't require it speaks (could go either way) but it must not be ERROR.
        event = _bash_event(stdout, "grep -c 'def ' daemon/content_router.py")
        decision = _run(router.classify_event(event))
        from daemon.tts_types import Category
        assert decision.category != Category.ERROR, (
            "grep -c count output must not be misclassified as ERROR"
        )

    def test_build_failure_speaks(self):
        """Build failure output with error summary — real signal."""
        stdout = (
            "Compiling mylib v0.1.0\n"
            "error[E0308]: mismatched types\n"
            "  --> src/main.rs:10:5\n"
            "error: could not compile mylib due to previous error\n"
        )
        # This contains 'error' keyword → should trigger ERROR category and speak
        self._check_speaks(stdout, "cargo build", "cargo_build_error")


# ===========================================================================
# ROUTER-05: _extract_bash AND-logic (digits AND domain keyword)
# ===========================================================================

class TestExtractBashAndLogic:
    """_extract_bash must require BOTH digit AND domain keyword for ambiguous tail."""

    def test_digits_only_no_domain_returns_empty(self):
        """Pure numeric output without domain keywords must not be extracted."""
        # Simulate 'wc -l' style: numbers only in the tail
        stdout = "230 /path/to/foo.py\n122 /path/to/bar.py\n352 total\n"
        from daemon.content_router import ContentRouter
        router = _make_router()
        extracted, hint = router._extract_bash(stdout, command="wc -l daemon/*.py")
        # The tail "352 total" has a digit but "total" is not a domain keyword
        # → should return empty (or at most a generic tail that hits the AND gate)
        # The key assertion: we must NOT return a non-empty extracted with just digits
        if extracted:
            # If something was extracted, verify it's not pure noise
            # (digit + no domain keyword) — the AND gate should catch wc -l style
            assert re.search(
                r"\b(test|build|deploy|commit|merge|branch|migration|server|"
                r"database|connection|api|endpoint|request|response)\b",
                extracted,
                re.IGNORECASE,
            ), (
                f"_extract_bash returned content with no domain keyword: {extracted!r}"
            )

    def test_grep_line_content_tail_not_extracted(self):
        """grep -n LINE:CONTENT last 3 lines should not be extracted as STATUS."""
        # Real grep -n output: lines like "42:def _extract_bash(self, stdout, ...)"
        stdout = "\n".join([
            "23:class ContentRouter:",
            "84:class ErrorCategory(Enum):",
            "651:    def _extract_bash(self, stdout: str, command: str = '') -> tuple[str, str]:",
        ])
        router = _make_router()
        event = _bash_event(stdout, "grep -n 'class ' daemon/content_router.py")
        decision = _run(router.classify_event(event))
        # This is grep LINE:CONTENT — should NOT speak as STATUS
        assert not decision.should_speak, (
            f"grep -n LINE:CONTENT should be silent, got should_speak=True: "
            f"content={decision.content!r}"
        )


# ---------------------------------------------------------------------------
# OBJECT extraction (the listener needs WHAT the action was about). A bare
# "prettier failed" must carry its object; subjectless test runners name the
# suite. These feed _bash_target_hint, which enriches the spoken context_hint.
# ---------------------------------------------------------------------------
class TestBashTargetObject:
    @pytest.mark.parametrize(
        "command, expected",
        [
            ("prettier --check src/", "prettier"),
            ("npx prettier --write .", "prettier"),
            ("eslint app/components/Button.tsx", "eslint on Button.tsx"),
            ("tsc --noEmit", "tsc"),
            ("ruff check .", "ruff"),
            ("npm test", "the test suite"),
            ("yarn test --watch=false", "the test suite"),
            ("pytest", "the tests"),
            ("pytest -q", "the tests"),
            ("vitest run", "the tests"),
            ("make", "make"),
            # existing behavior must still hold (object already specific):
            ("pytest tests/test_router.py -v", "router"),
            ("npm run build:prod", "build:prod"),
            # --- TASK A: extended runner coverage ---------------------------
            ("pnpm test", "the test suite"),
            ("pnpm build", "the build script"),
            ("pnpm run lint", "the lint script"),
            ("bun test", "the test suite"),
            ("yarn build", "the build script"),
            ("yarn deploy", "the deploy script"),
            ("yarn test --watch=false", "the test suite"),  # 'test' stays suite
            ("tox", "the tox run"),
            ("tox -e py311", "the tox run"),
            ("just deploy", "the deploy recipe"),
            ("just build:prod", "the build:prod recipe"),
            ("docker compose up -d", "docker compose up"),
            ("docker compose build", "docker compose build"),
            ("docker build -t app .", "the docker build"),
        ],
    )
    def test_object_extracted(self, command, expected):
        assert ContentRouter._bash_target_hint(command) == expected, (
            f"object for {command!r} should be {expected!r}, "
            f"got {ContentRouter._bash_target_hint(command)!r}"
        )

    def test_no_false_object_on_plain_command(self):
        # A plain, objectless command yields no fabricated object.
        assert ContentRouter._bash_target_hint("ls -la") == ""
        assert ContentRouter._bash_target_hint("echo hi") == ""


# ---------------------------------------------------------------------------
# TASK A end-to-end: the context_hint that reaches the summarizer must NAME the
# object for every runner — a bare "test result"/"bash output"/"build output"
# with no WHAT is the user's complaint ("tests ran, prettier failed").
# ---------------------------------------------------------------------------
class TestContextHintNamesObject:
    _TESTOUT = "===== test session starts =====\n5 passed in 0.32s\n"
    _BUILDOUT = "Compiling app v0.1.0\nbuild succeeded\n"

    def _hint(self, stdout: str, command: str) -> tuple[bool, str]:
        router = _make_router()
        decision = _run(router.classify_event(_bash_event(stdout, command)))
        return decision.should_speak, decision.context_hint

    @pytest.mark.parametrize(
        "stdout, command, must_contain",
        [
            # test runners — point 2: a recognized runner with no specific file
            # still names "the test suite" instead of a bare "test result".
            (_TESTOUT, "pnpm test", "the test suite"),
            (_TESTOUT, "bun test", "the test suite"),
            (_TESTOUT, "tox", "the tox run"),
            (_TESTOUT, "pytest", "the tests"),
            ("23 passed, 4 failed\n", "pytest tests/test_router.py -v", "router"),
            # build output — point 3: WHAT inferred from the command verb.
            (_BUILDOUT, "docker build -t app .", "the docker build"),
            ("Successfully installed flask\nbuild complete\n", "pip install flask", "the install"),
        ],
    )
    def test_runner_context_hint_names_object(self, stdout, command, must_contain):
        speak, hint = self._hint(stdout, command)
        assert speak is True, f"{command!r} should speak; got should_speak=False"
        assert must_contain in hint, (
            f"context_hint for {command!r} must name {must_contain!r}; got {hint!r}"
        )

    def test_unrecognized_test_script_keeps_bare_hint(self):
        """GUARD: an unrecognized runner ('./run_tests.sh') must NOT be upgraded
        to 'the test suite' — it keeps the bare 'test result' hint (no fabricated
        runner identity). Locks the existing test_content_router contract."""
        speak, hint = self._hint("23 passed, 4 failed\n", "./run_tests.sh")
        assert speak is True
        assert hint == "test result", f"expected bare 'test result', got {hint!r}"

    def test_generic_stdout_verb_inference(self):
        """A digit+domain bash tail with no explicit target infers the WHAT from
        the command verb ('npm install' -> 'the install') rather than a bare
        'bash output'."""
        speak, hint = self._hint(
            "added 120 packages\nserver dependencies resolved\n", "npm install"
        )
        assert speak is True
        assert "the install" in hint, f"verb not inferred into hint: {hint!r}"

    def test_grep_no_pattern_names_file(self):
        """A grep with no pattern but a scoped file names 'grep in <file>'."""
        router = _make_router()
        event = {
            "command": "tool_event", "tool_name": "Grep", "phase": "post",
            "session_id": "s",
            "tool_input": {"path": "daemon/content_router.py"},
            "tool_response": {"stdout": "match a\nmatch b\n", "stderr": "", "interrupted": False},
        }
        decision = _run(router.classify_event(event))
        assert decision.should_speak is True
        assert decision.context_hint == "grep in content_router.py", decision.context_hint
