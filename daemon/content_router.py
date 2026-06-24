"""ContentRouter — single classifier for the TTS pipeline overhaul (Wave 1.B).

Replaces ToolTenseManager + ClaudeOutputClassifier + IntelligentTTSFilter.
Routes tool_event / stop_event payloads (post-validation) into 4 categories:
ERROR | FINAL_ANSWER | INSIGHT | STATUS, plus an implicit "silence" default.

Architecture:
    classify_event(event_dict) -> RouterDecision        # pure-ish, no submission
    route(event_dict) -> Optional[RoutedItem]            # end-to-end with summarization

Pipeline:
    1. Schema sniff: tool_event vs stop_event
    2. Drop filter (regex): boilerplate / file paths / code blocks / dups
    3. Tool-result extractor (Bash/Grep/Glob/Task/WebFetch) → distilled signal
    4. Category mapping with regex patterns; ambiguous middle gets binary
       Ollama judgment (single SPEAK/SKIP token).
    5. Phrasing decision: <120 chars verbatim; ≥120 → mark for summarization.

Invariants:
    * Default verdict is silence (`should_speak=False`). Anything that does
      not match one of the four categories is dropped.
    * Never raises to caller. Malformed events return should_speak=False
      with reason captured in raw_excerpt.
    * Every Ollama call wrapped in asyncio.wait_for(timeout=1.0); on timeout,
      fall back to original content / heuristic verdict.
    * ERROR category uses raw stderr first; not aggressively summarized.
    * Pure modulo Ollama. Safe to call concurrently (only mutates
      a bounded recent-hash deque under no lock — accepts benign races).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Any, Optional, TYPE_CHECKING

from daemon.tts_types import (
    Category,
    RouterDecision,
    RoutedItem,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_ERROR,
    PRESSURE_MULTIPLIER,
    FlushCallback,
)

if TYPE_CHECKING:  # pragma: no cover — avoid circular imports at runtime
    from daemon.providers.base import LLMProvider
    from daemon.pipeline.queue_manager import QueueManager


log = logging.getLogger(__name__)


# ============================================================================
# Constants — detection patterns and thresholds
# ============================================================================

# Length cutoff: under = speak verbatim; at or above = mark for summarization
SUMMARIZE_THRESHOLD_CHARS = 120

# Bash extractor: minimum stdout length before a "no obvious signal" Bash
# event is considered worth the binary-LLM second opinion.
BASH_AMBIGUOUS_MIN_CHARS = 40

# Recent content-hash dedupe window
RECENT_HASH_LIMIT = 50

# Per-session recently-spoken window for the LLM-judge context signal. Small and
# bounded: we only care about the immediate "did we just say this?" horizon, not
# the full dedupe history. Distinct from RECENT_HASH_LIMIT (global dedupe).
RECENT_SPOKEN_PER_SESSION_LIMIT = 8

# Binary Ollama verdict timeout (the SPEAK/SKIP coin flip). Default raised so a
# real verdict can complete against the warm model; clamped to exceed the
# summarizer's inner cap in __init__ (R2/S4). The old 0.5s timed out before
# `model` could answer, defaulting borderline Bash to SKIP.
BINARY_LLM_TIMEOUT_S = 2.5
# Outer wrapper around summarize(). MUST be strictly greater than the
# summarizer's inner cap, else a slow call is cancelled by the wrapper and the
# RAW (unsummarized, markdown-laden) content is spoken. Clamped in __init__.
SUMMARIZE_WRAPPER_TIMEOUT_S = 4.0

# ERROR detection: stderr threshold (longer than this counts even without
# matching content keywords). Plan says ">10".
ERROR_STDERR_MIN_LEN = 10

# Tools we expect to handle specifically. Anything else: handled by a
# generic Bash-style fallback if signal is present, else silence.
TOOL_EXTRACTORS_REGISTERED = {"Bash", "Grep", "Glob", "Task", "WebFetch"}

# ROUTER-01 / ROUTER-06: Allowlist of categories that may produce speech.
# Any category NOT in this set is forced to should_speak=False by _make_decision.
# This enforces "default silence" — new Category values added to the enum are
# silenced automatically until explicitly added here.
_SPEAKABLE_CATEGORIES: frozenset[Category] = frozenset({
    Category.ERROR,
    Category.FINAL_ANSWER,
    Category.INSIGHT,
    Category.STATUS,
})

# Tools that are pure noise on success — never speak completion.
SILENT_SUCCESS_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})


# ----- Regex patterns -----------------------------------------------------

# Words that flag a result as an error even when stderr is empty.
# Word boundaries prevent matching inside identifiers like "ferror".
# `panic\w*` catches both "panic" and "panicked"; same for `fail\w*`/`error\w*`/
# `denied?`/`exception\w*` — covers verb tenses without false-broadening.
ERROR_KEYWORDS_RE = re.compile(
    r"\b(error\w*|exception\w*|fail\w*|deny|denied|traceback|panic\w*|"
    r"fatal|cannot|no\s+such|not\s+found)\b",
    re.IGNORECASE,
)

# Insight markers — catch the canonical "★ Insight" tag plus the natural
# phrasings the assistant tends to use when realizing something.
INSIGHT_MARKER_RE = re.compile(r"★\s*Insight", re.IGNORECASE)
INSIGHT_LEADING_RE = re.compile(
    r"^(I\s+see|I\s+found|The\s+(root\s+cause|fix|solution|issue))",
    re.IGNORECASE,
)
INSIGHT_INLINE_RE = re.compile(
    r"\b(turns\s+out|it\s+seems\s+that|this\s+means)\b",
    re.IGNORECASE,
)

# pytest / unittest / cargo test counts — high-signal STATUS for Bash output.
TEST_RESULT_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|skipped|errors?)\b",
    re.IGNORECASE,
)

# Build / compile chatter
BUILD_RESULT_RE = re.compile(
    r"\b(compiled|warnings?:\s*\d+|errors?:\s*\d+|"
    r"build\s+(succeeded|failed|complete))\b",
    re.IGNORECASE,
)

# Stuff we always drop on sight.
NOISE_PREFIX_RE = re.compile(
    r"^(Here\s+is|Here's|This\s+is|Below\s+(is|are)|The\s+following|"
    r"Looking\s+at|Let\s+me|I'll|I\s+will|To\s+begin|In\s+order\s+to)",
    re.IGNORECASE,
)

# Fenced code blocks; if the content IS a code block, drop it.
CODE_FENCE_RE = re.compile(r"^\s*```")

# System-reminder tags — never speak these.
SYSTEM_REMINDER_RE = re.compile(r"<\s*system[-_]reminder", re.IGNORECASE)

# File-path-only content (e.g., "src/foo/bar.py") — usually paste-noise.
PATH_ONLY_RE = re.compile(r"^[\w./\\\-]+\.\w{1,8}$")

# Wave 2.5 tuning: false-positive guards observed in shadow.log analysis.

# `ls -la` output lines — start with a file-mode triplet (e.g., "srw-rw-rw-",
# "drwxr-xr-x", "lrwxr-xr-x"), then optional extended-attr marker (@/./+),
# then link count + owner. We never want to speak file listings.
FILE_LISTING_RE = re.compile(
    r"^[-dlcbspw][-rwxstST]{9}[@.+]?\s+\d+\s+\S+\s+",
    re.MULTILINE,
)

# Git commit log lines — hex hash prefix OR bracketed-branch prefix.
# "6a85473 test(01-02): add failing tests" contains "failing" but is not
# an error — it's a commit message describing what a test asserts.
# "[worktree-agent-... 1d78dc3] test(...): ..." is git's worktree commit
# output (`git commit` in a worktree prints "[branch hash] message").
GIT_COMMIT_LINE_RE = re.compile(
    r"^(?:\s*[a-f0-9]{6,12}\s+\S|\s*\[[\w\-/]+\s+[a-f0-9]{6,12}\]\s+\S)",
    re.MULTILINE,
)

# HOTFIX 2026-05-05: git diff --stat output (`file.py | 71 ++++++++--`) AND
# git diff --shortstat (`5 files changed, 62 insertions(+), 9 deletions(-)`).
# Reading "plus plus plus..." for the change visualization is catastrophic;
# reading the path verbatim is also useless. Drop all of it.
GIT_DIFF_STAT_RE = re.compile(
    r"(?:^\s*\S+\s+\|\s+\d+\s+[+\-]{2,}|\b\d+\s+files?\s+changed\b)",
    re.MULTILINE,
)

# HOTFIX 2026-05-05: any run of 5+ identical punctuation/symbol chars
# (e.g., "+++++", "-----", "=====", "*****", "~~~~~") is unspeakable noise.
# Pattern catches diff visualizations, banners, ASCII separators, etc.
SYMBOL_RUN_RE = re.compile(r"([+\-=*#~_<>])\1{4,}")

# "warning:" / "warn:" / "deprecated:" prefixes — should NOT trigger ERROR.
# These are advisory, not failures. The cargo example was
# "warning: Failed to clone files; falling back to full copy."
WARNING_PREFIX_RE = re.compile(
    r"^\s*(warning|warn|deprecated)[:!]",
    re.IGNORECASE | re.MULTILINE,
)

# ROUTER-02 / ROUTER-04: grep -n LINE:CONTENT pattern.
# Matches lines like "42:def foo():" or "123:    class Bar:" — grep line-number
# output. Never useful to speak; often hits ERROR_KEYWORDS_RE on identifiers
# like "ErrorCategory". Must be checked on RAW stdout before shape mutation.
GREP_LINE_RE = re.compile(
    r"^\s*\d+:\S",  # integer + colon + non-whitespace at line start
    re.MULTILINE,
)

# ROUTER-02: wc -l output — "  230 path/to/file.ext" style.
# Matches lines where a leading integer is followed by a filepath.
# We check for ≥2 such lines to avoid false-positives on single-line output.
WC_OUTPUT_RE = re.compile(
    r"^\s*\d+\s+\S+\.\w+\s*$",
    re.MULTILINE,
)

# ROUTER-02: git status branch/tracking lines and unstaged/staged markers.
# Lines like "On branch main", "Your branch is up to date with...",
# "Changes not staged...", "Untracked files:", "  modified:", "  deleted:".
GIT_STATUS_NOISE_RE = re.compile(
    r"^(?:On branch|Your branch|Changes (?:not staged|to be committed)|"
    r"Untracked files:|nothing to commit|no changes added|"
    r"\s+(?:modified|deleted|renamed|new file|both modified):)",
    re.MULTILINE,
)


def _drop_check_raw(content: str) -> Optional[str]:
    """ROUTER-04: Run the drop filter on RAW stdout BEFORE _extract_bash mutates it.

    This is the key fix for F3 / ROUTER-04: previously, _drop_check() ran on
    the *extracted tail* (last 3 lines from _extract_bash), which meant that
    FILE_LISTING_RE, GREP_LINE_RE, and WC_OUTPUT_RE patterns never got a
    chance to match the multi-line header that makes the output identifiable.

    Returns the drop-reason string if the content should be dropped, else None.

    Called from _classify_tool() on raw stdout BEFORE _extract_tool_signal().
    """
    if not content:
        return None  # empty stdout — let the normal path handle it

    # File listings: ls -la, ls -l, find with -ls flag.
    # FILE_LISTING_RE matches on the full raw content (multi-line).
    if FILE_LISTING_RE.search(content):
        return "file-listing content (ls -la raw)"

    # grep -n LINE:CONTENT: "42:def foo():" — numeric line prefix + colon.
    # Check ≥2 lines to avoid false-positives (a single grep result can be
    # a meaningful find, e.g. "1:ERROR: config missing").
    grep_hits = len(GREP_LINE_RE.findall(content))
    if grep_hits >= 2:
        return "grep-line-numbered content (LINE:CONTENT noise)"

    # wc -l multi-file output: "230 path/to/foo.py"
    wc_hits = len(WC_OUTPUT_RE.findall(content))
    if wc_hits >= 2:
        return "wc -l multi-file output"

    # git status output — pure working-tree noise.
    if GIT_STATUS_NOISE_RE.search(content):
        return "git status output"

    # git diff --stat / git show --stat (already caught by GIT_DIFF_STAT_RE
    # in the instance _drop_check, but we also check here on the full raw
    # content before any tail trimming occurs).
    if GIT_DIFF_STAT_RE.search(content):
        return "git-diff-stat content (raw)"

    # find output: multi-line list of paths (no mode strings like ls -la).
    # Heuristic: ≥5 lines, >80% start with ./ or / and end with an extension
    # or no extension (bare directory path).
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if len(lines) >= 5:
        path_lines = sum(
            1 for ln in lines
            if ln.strip().startswith("./") or ln.strip().startswith("/")
        )
        if path_lines / len(lines) > 0.8:
            return "find-output path list (raw)"

    return None


def _looks_like_path_list(content: str) -> bool:
    """True if content reads like a list of slash-paths (e.g., a URL route
    table dump: '/docs/oauth2-redirect /redoc /healthz'). >60% slash-tokens
    AND no surrounding sentence prose.
    """
    tokens = content.split()
    if len(tokens) < 2 or len(tokens) > 30:
        return False
    path_like = sum(1 for t in tokens if t.startswith("/") or t.startswith("./"))
    return path_like / len(tokens) > 0.6

# Domain keywords that suggest a Bash output line is worth surfacing
# (used by the "ambiguous Bash" branch).
DOMAIN_KEYWORDS_RE = re.compile(
    r"\b(test|build|deploy|commit|merge|branch|migration|server|"
    r"database|connection|api|endpoint|request|response)\b",
    re.IGNORECASE,
)


# ============================================================================
# Helpers
# ============================================================================

def _hash_content(text: str) -> str:
    """Stable short hash for dedupe — case-insensitive, whitespace-collapsed."""
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


def _now() -> float:
    return time.time()


def _last_n_lines(text: str, n: int = 3) -> str:
    """Return the last `n` non-empty lines joined by spaces."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return " ".join(lines[-n:])


def _truncate(text: str, max_chars: int = 80) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


# ============================================================================
# ContentRouter
# ============================================================================

class ContentRouter:
    """Single classifier replacing the legacy 3-classifier stack.

    Construction is cheap; instantiate once per daemon lifetime.
    The ``queue_manager`` is optional and may be late-bound via
    :py:meth:`set_queue_manager` (W3.B wires this to avoid a circular dep).
    """

    def __init__(
        self,
        config: dict,
        provider: Optional["LLMProvider"] = None,
        queue_manager: Optional["QueueManager"] = None,
    ) -> None:
        self.config = config or {}
        self.provider = provider
        self.queue_manager = queue_manager

        # Tunables — read from config with sane defaults.
        routing_cfg = self.config.get("routing", {}) if isinstance(self.config, dict) else {}
        self.summarize_threshold = int(
            routing_cfg.get("summarize_threshold_chars", SUMMARIZE_THRESHOLD_CHARS)
        )
        self.bash_ambiguous_min = int(
            routing_cfg.get("bash_ambiguous_min_chars", BASH_AMBIGUOUS_MIN_CHARS)
        )

        # R2/S4: Ollama call budgets. The summarizer's inner cap is the binding
        # constraint; the outer wrappers here MUST exceed it, otherwise a slow
        # call is cancelled by the wrapper before the inner (markdown-clean)
        # fallback runs — and the RAW markdown content gets spoken. We clamp to
        # enforce wrapper > inner regardless of what the config says, so a
        # mis-set config can never silently re-introduce raw-markdown readout.
        _inner = float(getattr(self.provider, "inner_timeout_s", 2.0) or 2.0)
        self.summarize_timeout_s = max(
            float(routing_cfg.get("summarize_timeout_s", SUMMARIZE_WRAPPER_TIMEOUT_S)),
            _inner + 1.5,
        )
        self.binary_judge_timeout_s = max(
            float(routing_cfg.get("binary_judge_timeout_s", BINARY_LLM_TIMEOUT_S)),
            _inner + 0.5,
        )

        # Recent-hash dedupe — bounded deque, accepts benign concurrency races.
        self._recent_hashes: deque[str] = deque(maxlen=RECENT_HASH_LIMIT)

        # Per-session recently-spoken window (R-context): a small bounded deque
        # per session, mirroring _recent_hashes but scoped so the LLM judge can
        # tell "is THIS content fresh in THIS session, or did we just speak the
        # same pattern moments ago?" Updated only when an item is actually noted
        # for speech (see _note_hash). Same benign-race tolerance as above.
        self._recent_spoken_by_session: dict[str, deque[str]] = {}

        # RECALL INSTRUMENTATION 2026-06-19: last drop reason, read by the
        # daemon's shadow logger when route() returns None. Same benign-race
        # tolerance as _recent_hashes above (worst case: a concurrent skip's
        # reason is mislabeled — diagnostic data, not correctness). Without
        # this, route() collapses every skip to None and the *why* (drop-check
        # vs backpressure vs dedup) is unrecoverable from the log.
        self._last_drop_reason: str = ""

        # ---- TurnBuffer plumbing (W2.C) ----
        # Per-session lazy-created TurnBuffers; cached so add() can be called
        # repeatedly without rebuilding the buffer state.
        self._turn_buffers: dict[str, "TurnBuffer"] = {}
        # Flush callback wired by W2.A after construction. Without it,
        # turn_buffer_for() raises — explicit fail-safe so we never silently
        # drop batches because the wiring step was forgotten.
        self._turn_buffer_flush_callback: Optional[FlushCallback] = None
        # Idle window — TurnBuffer flushes after this many ms of silence.
        # Configurable via routing.turn_buffer_idle_ms (default 800ms per spec).
        self._turn_buffer_idle_ms = int(
            routing_cfg.get("turn_buffer_idle_ms", 800)
        )

        log.debug(
            "ContentRouter ready (summarize_threshold=%d, bash_min=%d, qm=%s)",
            self.summarize_threshold, self.bash_ambiguous_min,
            "bound" if queue_manager else "deferred",
        )

    # ------------------------------------------------------------------ API

    def set_queue_manager(self, qm: "QueueManager") -> None:
        """Late-bind QueueManager (W3.B avoids circular dep at construction)."""
        self.queue_manager = qm
        log.debug("ContentRouter: QueueManager bound")

    # ---- TurnBuffer wiring & lazy accessor (W2.C) ----------------------

    def set_turn_buffer_callback(self, cb: FlushCallback) -> None:
        """Wire the per-batch flush callback used by every TurnBuffer this
        router lazily creates.

        W2.A calls this once after construction with a coroutine function that
        hands a list of RoutedItem off to the pipeline (typically
        ``pipeline_adapter.submit_many``-style). Without it,
        ``turn_buffer_for()`` raises — that's an explicit fail-safe so a
        forgotten wiring step never silently drops batches.
        """
        self._turn_buffer_flush_callback = cb
        log.debug("ContentRouter: TurnBuffer flush callback wired")

    def turn_buffer_for(self, session_id: str) -> "TurnBuffer":
        """Lazy-create (or return cached) TurnBuffer for ``session_id``.

        Note: ``ContentRouter.route()`` does NOT auto-add to the buffer.
        The caller (the daemon's socket handler) decides batching vs. direct
        submission based on category — ERROR and FINAL_ANSWER bypass the
        buffer entirely (instant playback) while INSIGHT/STATUS go through
        the buffer for per-turn batching.

        Raises:
            RuntimeError: if ``set_turn_buffer_callback`` was never called.
        """
        if self._turn_buffer_flush_callback is None:
            raise RuntimeError(
                "TurnBuffer flush callback not wired "
                "(call ContentRouter.set_turn_buffer_callback first)"
            )
        buf = self._turn_buffers.get(session_id)
        if buf is None:
            buf = TurnBuffer(
                session_id=session_id,
                flush_callback=self._turn_buffer_flush_callback,
                idle_window_ms=self._turn_buffer_idle_ms,
            )
            self._turn_buffers[session_id] = buf
        return buf

    async def classify_event(self, event: dict) -> RouterDecision:
        """Apply regex/heuristic ladder + (optional) Ollama judgment.

        Args:
            event: dict matching TOOL_EVENT_SCHEMA or STOP_EVENT_SCHEMA.

        Returns:
            RouterDecision. ``should_speak=False`` signals drop.

        Never raises — malformed events return should_speak=False
        with reason captured in ``raw_excerpt``.
        """
        try:
            return await self._classify_inner(event)
        except Exception as exc:  # noqa: BLE001 — never escape to socket handler
            log.exception("ContentRouter.classify_event: unexpected failure")
            return self._silence(
                event,
                reason=f"classifier crash: {type(exc).__name__}: {exc}",
            )

    async def route(self, event: dict) -> Optional[RoutedItem]:
        """End-to-end: classify → (maybe) summarize → wrap in RoutedItem.

        Returns None if classify_event returns should_speak=False.

        Backpressure (W3.B): when QueueManager is bound, the session's
        current pressure multiplier raises the effective decision threshold
        so borderline content stops being submitted at all. ERROR-class
        items always speak regardless of pressure.

        Two backpressure effects layer in:
          1. **Priority gate** — items with ``priority < pressure * 2`` are
             silenced. With the canonical PRESSURE_MULTIPLIER table:
                GREEN  (1.0) → cutoff 2  → no items dropped
                YELLOW (1.5) → cutoff 3  → no items dropped (LOW=3 still passes)
                RED    (2.5) → cutoff 5  → drops PRIORITY_LOW (3); NORMAL(5)+ passes
                BLACK  (5.0) → cutoff 10 → drops everything except ERROR(10)
          2. **Summarization push** — even for items that survive the gate,
             the effective summarize threshold is divided by pressure so
             borderline-long content gets condensed earlier under load.
        """
        decision = await self.classify_event(event)
        if not decision.should_speak:
            # raw_excerpt holds the drop reason (see _silence). Surface it for
            # the shadow logger so recall losses are diagnosable.
            self._last_drop_reason = decision.raw_excerpt or "classifier: should_speak=False"
            return None

        pressure = self._pressure_for(event)

        # 1. Priority gate — silence borderline content under high pressure.
        #    ERROR always passes regardless of pressure.
        if (
            decision.category != Category.ERROR
            and pressure > 1.0
            and decision.priority < pressure * 2
        ):
            log.debug(
                "route: silenced %s priority=%d under pressure=%.1f (cutoff=%.1f)",
                decision.category.value, decision.priority,
                pressure, pressure * 2,
            )
            self._last_drop_reason = (
                f"backpressure: {decision.category.value} priority "
                f"{decision.priority} < cutoff {pressure * 2:.0f} (pressure {pressure:.1f})"
            )
            return None

        # 2. Summarization push — longer messages get pushed harder toward
        #    summary when the pipeline is already lagging. ERRORs are exempt.
        if (
            decision.category != Category.ERROR
            and pressure > 1.0
            and not decision.needs_summarization
            and len(decision.content) >= self.summarize_threshold / pressure
        ):
            decision = replace(decision, needs_summarization=True)

        if decision.needs_summarization:
            decision = await self._maybe_summarize(decision)

        session_id = event.get("session_id") or "default"
        return RoutedItem(decision=decision, session_id=session_id)

    # ============================================================ internals

    async def _classify_inner(self, event: dict) -> RouterDecision:
        if not isinstance(event, dict):
            return self._silence(event, reason="event is not a dict")

        command = event.get("command")
        if command == "stop_event":
            return self._classify_stop(event)
        if command == "tool_event":
            return await self._classify_tool(event)

        # Unknown command (or missing) → silence.
        return self._silence(event, reason=f"unknown command: {command!r}")

    # ------- Stop events -----------------------------------------------

    def _classify_stop(self, event: dict) -> RouterDecision:
        content_raw = (event.get("content") or "").strip()
        if not content_raw:
            return self._silence(event, reason="stop_event with empty content")

        # Drop filter on the assistant text itself
        dropped = self._drop_check(content_raw)
        if dropped is not None:
            return self._silence(event, reason=dropped)

        # INSIGHT vs FINAL_ANSWER decision: insight markers / phrasings win,
        # otherwise it's the final answer for the turn.
        if (
            INSIGHT_MARKER_RE.search(content_raw)
            or INSIGHT_LEADING_RE.search(content_raw)
            or INSIGHT_INLINE_RE.search(content_raw)
        ):
            category = Category.INSIGHT
            priority = PRIORITY_NORMAL
            context_hint = "assistant insight"
        else:
            category = Category.FINAL_ANSWER
            priority = PRIORITY_HIGH
            context_hint = "final answer"

        # Track for dedupe (don't re-speak identical assistant message)
        self._note_hash(content_raw, event.get("session_id") or "default")

        # HOTFIX 2026-05-05: honor the documented "≥120 chars → summarize" rule.
        # Without this, FINAL_ANSWER under GREEN pressure was read verbatim,
        # backing up the audio queue with multi-paragraph assistant prose.
        needs_sum = len(content_raw) >= self.summarize_threshold

        return self._make_decision(
            event=event,
            should_speak=True,
            category=category,
            content=content_raw,
            priority=priority,
            context_hint=context_hint,
            needs_summarization=needs_sum,
        )

    # ------- Tool events -----------------------------------------------

    async def _classify_tool(self, event: dict) -> RouterDecision:
        # Pre-tool events never speak — they're for the daemon's accounting only.
        # (Long-running announcement is the pre-tool hook script's job, not ours.)
        if event.get("phase") == "pre":
            return self._silence(event, reason="pre-tool phase: silence by policy")

        tool_name = event.get("tool_name") or ""
        response = event.get("tool_response") or {}
        if not isinstance(response, dict):
            return self._silence(event, reason="tool_response is not a dict")

        stdout = (response.get("stdout") or "").strip()
        stderr = (response.get("stderr") or "").strip()
        interrupted = bool(response.get("interrupted"))

        # ----- ERROR detection (priority 1) -----
        # stderr non-empty + length>10 OR matches keyword OR interrupted=true
        # OR keyword pattern hits stdout (some tools route errors to stdout).
        # Important: test-result lines like "23 passed, 4 failed" naturally
        # contain "failed" — suppress stdout-keyword ERROR detection when a
        # test-results pattern is also present. stderr keywords still fire.
        # Wave 2.5 tuning: also suppress when content looks like a warning
        # ("warning: Failed to clone…") or a git commit log line ("6a85473
        # test(…): add failing tests…") — those keyword hits are not errors.
        is_error = False
        error_content = ""
        stdout_looks_like_tests = bool(stdout and TEST_RESULT_RE.search(stdout))

        def _is_error_false_positive(text: str) -> bool:
            """True if the keyword match is a warning prefix, git commit log,
            or grep LINE:CONTENT output (e.g., '84:class ErrorCategory(Enum):').

            ROUTER-02: grep -n output where a line number prefix precedes an
            identifier containing an error keyword must not trigger ERROR.
            We check for ≥2 such lines (a whole grep result set) OR a single
            line if the ENTIRE content is a single grep-line match.
            """
            if not text:
                return False
            if WARNING_PREFIX_RE.search(text):
                return True
            if GIT_COMMIT_LINE_RE.search(text):
                return True
            # Screen grep LINE:CONTENT: ≥1 "NUM:identifier" line means the
            # keyword hit is inside a grep output context, not a real error.
            if GREP_LINE_RE.search(text):
                return True
            return False

        if stderr and len(stderr) > ERROR_STDERR_MIN_LEN:
            # Long stderr is usually a real error — but a stderr that's ONLY
            # warnings or git output (e.g., 'git commit' prints to stderr) is
            # not. Skip ERROR if the stderr is dominated by those patterns.
            if not _is_error_false_positive(stderr):
                is_error = True
                error_content = stderr
        elif interrupted:
            is_error = True
            error_content = stderr or stdout or "tool was interrupted"
        elif ERROR_KEYWORDS_RE.search(stderr or "") and not _is_error_false_positive(stderr or ""):
            is_error = True
            error_content = stderr
        elif (
            not stdout_looks_like_tests
            and stdout
            and ERROR_KEYWORDS_RE.search(stdout)
            and not _is_error_false_positive(stdout)
        ):
            is_error = True
            error_content = self._extract_error_snippet(stdout)

        if is_error:
            content = error_content.strip() or "tool reported an error"
            # ERRORs aren't aggressively summarized; only summarize if very long.
            needs_sum = len(content) >= self.summarize_threshold * 3
            return self._make_decision(
                event=event,
                should_speak=True,
                category=Category.ERROR,
                content=content,
                priority=PRIORITY_ERROR,
                context_hint=f"{tool_name} error" if tool_name else "tool error",
                needs_summarization=needs_sum,
            )

        # ----- Silent-success tools -----
        # Read/Edit/Write succeeding is not a finding worth speaking.
        if tool_name in SILENT_SUCCESS_TOOLS:
            return self._silence(
                event, reason=f"{tool_name} success is silent by policy"
            )

        # ----- ROUTER-04: raw-stdout drop check BEFORE shape mutation -----
        # _drop_check_raw operates on the FULL raw stdout so that multi-line
        # patterns (ls -la mode strings, grep LINE:CONTENT, wc -l rows, git
        # status boilerplate) are visible BEFORE _extract_bash trims to the
        # last 3 lines. This is the F3/ROUTER-04 fix: previously the drop
        # check only saw the extracted tail, which stripped the very lines
        # that the pattern matchers relied on.
        if stdout:
            raw_drop = _drop_check_raw(stdout)
            if raw_drop is not None:
                return self._silence(event, reason=f"raw-stdout: {raw_drop}")

        # ----- Per-tool extractors -----
        extracted, hint = self._extract_tool_signal(tool_name, event, stdout)

        # If extractor found a signal, classify as STATUS straightaway.
        if extracted:
            # Drop filter (e.g., dup with an earlier identical extraction).
            dropped = self._drop_check(extracted)
            if dropped is not None:
                return self._silence(event, reason=dropped)
            self._note_hash(extracted, event.get("session_id") or "default")
            return self._make_decision(
                event=event,
                should_speak=True,
                category=Category.STATUS,
                content=extracted,
                priority=PRIORITY_NORMAL,
                context_hint=hint or f"{tool_name} result",
            )

        # ----- Ambiguous middle: substantive-looking Bash output -----
        # Long-enough content, contains numbers AND a domain keyword,
        # but didn't match a structured extractor. Ask Ollama for a binary
        # SPEAK/SKIP verdict.
        # ROUTER-05: require AND (digits AND domain keyword) instead of OR.
        # OR was too loose: `lsof | head` (digits, no domain keyword) and
        # tool outputs with only path/numbers were triggering the LLM judge
        # and sometimes escaping as STATUS. Both signals must be present.
        if (
            stdout
            and len(stdout) >= self.bash_ambiguous_min
            and re.search(r"\d", stdout)
            and DOMAIN_KEYWORDS_RE.search(stdout)
        ):
            # Enrich the judge with compact session/project context built from
            # signals ALREADY on the event (command target, project dir, plus a
            # session-local recency flag). This only widens the judge's view —
            # it does NOT bypass any gate above (markup/is_speakable/drop-check/
            # gibberish + digit+keyword requirement all already passed).
            session_id = event.get("session_id") or "default"
            recently_spoken = self._was_recently_spoken(stdout, session_id)
            judge_context = self._judge_context(event, recently_spoken)
            verdict = await self._binary_llm_judge(
                stdout, tool_name, judge_context=judge_context
            )
            if verdict:
                # Use the last few lines as the content payload.
                content = _last_n_lines(stdout, 3) or stdout[:240]
                dropped = self._drop_check(content)
                if dropped is not None:
                    return self._silence(event, reason=dropped)
                # recency_key=stdout so the per-session recency check (which
                # hashes the full stdout, available pre-trim) matches what we
                # store here, even though global dedupe keys on the trimmed
                # spoken content.
                self._note_hash(content, session_id, recency_key=stdout)
                # Name the WHAT instead of a bare "<tool> output": prefer a
                # specific bash target, then a verb-inferred object, else fall
                # back to the generic tool-output hint.
                tool_input = event.get("tool_input") or {}
                cmd = (tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
                obj = self._bash_target_hint(cmd) or self._infer_command_what(cmd)
                hint = f"{tool_name} output from {obj}" if obj else f"{tool_name} output"
                return self._make_decision(
                    event=event,
                    should_speak=True,
                    category=Category.STATUS,
                    content=content,
                    priority=PRIORITY_LOW,
                    context_hint=hint,
                )

        # Default: silence.
        return self._silence(
            event,
            reason=f"no signal from {tool_name or 'unknown tool'}",
        )

    # ------- Tool-result extractors --------------------------------------

    def _extract_tool_signal(
        self,
        tool_name: str,
        event: dict,
        stdout: str,
    ) -> tuple[str, str]:
        """Per-tool extractor → (extracted_content, context_hint).

        Returns ('', '') when the tool produced no substantive signal.

        Wave 2.5 enrichment: context_hint now includes a short command-target
        descriptor when available (e.g., "test result from test_router" instead
        of just "test result"), so model's summary can incorporate WHAT was
        being tested rather than producing bare counts.
        """
        if tool_name == "Bash":
            tool_input = event.get("tool_input") or {}
            command = (tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
            return self._extract_bash(stdout, command=command)
        if tool_name in ("Grep", "Glob"):
            return self._extract_grep(event, stdout)
        if tool_name == "Task":
            return self._extract_task(stdout)
        if tool_name == "WebFetch":
            return self._extract_webfetch(stdout)
        # Unknown tool — generic: use last meaningful line if there is one.
        if stdout and BUILD_RESULT_RE.search(stdout):
            return _last_n_lines(stdout, 2), f"{tool_name or 'tool'} output"
        return "", ""

    def _extract_bash(self, stdout: str, command: str = "") -> tuple[str, str]:
        if not stdout:
            return "", ""

        target_hint = self._bash_target_hint(command)

        # Test runner output (pytest / cargo test / unittest / jest)
        test_matches = list(TEST_RESULT_RE.finditer(stdout))
        if test_matches:
            # Reconstruct an at-most-three-fact summary like "23 passed, 4 failed".
            seen = []
            for m in test_matches:
                phrase = f"{m.group(1)} {m.group(2).lower()}"
                if phrase not in seen:
                    seen.append(phrase)
                if len(seen) >= 3:
                    break
            content = ", ".join(seen) + "."
            content = self._maybe_prefix_target(content, target_hint)
            if target_hint:
                hint = f"test result from {target_hint}"
            elif self._is_test_command(command):
                # Recognized test runner but no specific file/module named — still
                # give the listener an object instead of a bare "test result".
                hint = "the test suite"
            else:
                # Unrecognized runner (e.g. './run_tests.sh') — keep bare hint.
                hint = "test result"
            return content, hint

        # Build / compile output
        if BUILD_RESULT_RE.search(stdout):
            content = _last_n_lines(stdout, 2)
            content = self._maybe_prefix_target(content, target_hint)
            if target_hint:
                hint = f"build output from {target_hint}"
            else:
                # No explicit target — infer the WHAT from the command verb
                # (install/build/lint/...) so the hint names an object. Falls
                # back to bare "build output" only when nothing is inferable.
                inferred = self._infer_command_what(command)
                hint = f"build output from {inferred}" if inferred else "build output"
            return content, hint

        # Otherwise return the last 3 non-empty lines IF there's BOTH a digit
        # AND a domain keyword to make it worth speaking.
        # ROUTER-05: changed from OR to AND — previously a digit alone (e.g.,
        # `lsof | head` numbers, `wc -l` counts) could escape as STATUS.
        # Both signals must be present: the number provides the "what changed"
        # and the domain keyword provides the "why it matters" context.
        tail = _last_n_lines(stdout, 3)
        if tail and re.search(r"\d", tail) and DOMAIN_KEYWORDS_RE.search(tail):
            content = self._maybe_prefix_target(tail, target_hint)
            if target_hint:
                hint = f"bash output from {target_hint}"
            else:
                # Infer the WHAT from the command verb (install/build/lint/
                # license/metrics/branch/...) rather than a bare "bash output".
                inferred = self._infer_command_what(command)
                hint = f"bash output from {inferred}" if inferred else "bash output"
            return content, hint

        return "", ""

    @staticmethod
    def _verbosity_for(content: str, target_hint: str) -> str:
        """Per-output verbosity classifier — lightweight, no LLM call.

        Returns 'terse' if the content is already self-explanatory (no
        context prefix needed) or 'targeted' if it would benefit from the
        tool target being woven in.

        Heuristics (any one disqualifies for 'targeted'):
          * Content >= 60 chars  → likely carries its own context
          * Names a filename ('foo.py', 'bar.rs')
          * Names a code-symbol path ('queue::manager', 'foo::bar::baz')
          * Names a line reference ('line 42')
          * Already contains the target_hint string

        Otherwise, if we have a target_hint, return 'targeted'. The caller
        can then prepend natural context like "In test_router: ...".
        """
        if not target_hint:
            return "terse"
        if len(content) >= 60:
            return "terse"
        # Already mentions the target — no need to prefix.
        if target_hint.lower() in content.lower():
            return "terse"
        # Specific refs: filename / rust path / line number.
        if re.search(
            r"\b\w+\.(py|js|ts|rs|go|sh|md|toml|json|yaml|yml)\b"
            r"|\b\w+::\w+\b"
            r"|\bline\s+\d+\b",
            content,
        ):
            return "terse"
        return "targeted"

    @staticmethod
    def _maybe_prefix_target(content: str, target_hint: str) -> str:
        """Apply target prefix when verbosity says it's worth it. Keeps the
        result naturally readable: 'In test_router: 23 passed, 4 failed.'

        For multi-line content (build output tail), prefix only the first line
        to avoid awkward duplication.
        """
        if not content:
            return content
        verbosity = ContentRouter._verbosity_for(content, target_hint)
        if verbosity != "targeted":
            return content
        # Single-line: prepend.  Multi-line: prepend with separator.
        if "\n" in content:
            first, rest = content.split("\n", 1)
            return f"In {target_hint}: {first}\n{rest}"
        return f"In {target_hint}: {content}"

    @staticmethod
    def _bash_target_hint(command: str) -> str:
        """Extract a short human-readable target from a bash command.

        Returns the test file basename, build target, npm script name, etc.
        Empty string if no salient target can be identified. Used to enrich
        context_hint passed to the OllamaSummarizer so model can include
        WHAT was being run in its spoken summary.

        Examples:
            "pytest tests/test_router.py -v"     → "test_router"
            "npm run build:prod"                 → "build:prod"
            "cargo test queue::manager"          → "queue::manager"
            "make install"                       → "install"
            "python3 scripts/foo.py --flag x"    → "foo"
        """
        if not command:
            return ""
        cmd = command.strip()

        # pytest <target>
        m = re.search(r"\bpytest\s+([^\s-][^\s]*)", cmd)
        if m:
            tok = m.group(1)
            base = tok.rsplit("/", 1)[-1] if "/" in tok else tok
            base = re.sub(r"\.(py|js|ts|rs|go)$", "", base)
            base = re.sub(r"^test_", "", base)
            return base or ""

        # npm run <script>  /  npm test / npm test -- <args>
        m = re.search(r"\bnpm\s+run\s+(\S+)", cmd)
        if m:
            return m.group(1)

        # cargo test <module::path>  /  cargo build  /  cargo run
        m = re.search(r"\bcargo\s+test\s+([\w:]+)", cmd)
        if m:
            return m.group(1)
        m = re.search(r"\bcargo\s+(build|run|check|clippy)", cmd)
        if m:
            return f"cargo {m.group(1)}"

        # make <target>
        m = re.search(r"\bmake\s+([\w./-]+)", cmd)
        if m:
            return m.group(1)

        # go test <package>
        m = re.search(r"\bgo\s+test\s+(\S+)", cmd)
        if m:
            return m.group(1)

        # python3 path/to/script.py — script basename
        m = re.search(r"\bpython3?\s+([\w./-]+\.py)\b", cmd)
        if m:
            tok = m.group(1)
            base = tok.rsplit("/", 1)[-1] if "/" in tok else tok
            return re.sub(r"\.py$", "", base)

        # tox — name the run explicitly (the suite object is "the tox run").
        # Checked before the generic test-runner group below so "tox" doesn't
        # collapse to the vaguer "the tests".
        if re.search(r"\btox\b", cmd):
            return "the tox run"

        # docker compose <subcommand> / docker build — name the docker action.
        # "docker compose up" → "docker compose up"; "docker build ..." →
        # "the docker build". The compose subcommand (up/down/build/run) IS the
        # object the listener cares about.
        m = re.search(r"\bdocker\s+compose\s+([a-z][\w-]*)", cmd)
        if m:
            return f"docker compose {m.group(1)}"
        if re.search(r"\bdocker\s+build\b", cmd):
            return "the docker build"

        # bun test → name the suite (subjectless test runner, like npm/pnpm test).
        if re.search(r"\bbun\s+test\b", cmd):
            return "the test suite"

        # pnpm/yarn <script> (build, lint, deploy, etc., NOT 'test' which the
        # subjectless block below names "the test suite"). "pnpm run lint" /
        # "pnpm lint" / "yarn build" → "the lint script" / "the build script".
        m = re.search(r"\b(?:pnpm|yarn)\s+(?:run\s+)?([a-z][\w:-]*)", cmd)
        if m and m.group(1) != "test":
            return f"the {m.group(1)} script"

        # just <recipe> — the recipe name IS the object. "just deploy" → "the
        # deploy recipe". A bare "just" (no recipe) falls through to no object.
        m = re.search(r"\bjust\s+([a-z][\w:-]*)", cmd)
        if m:
            return f"the {m.group(1)} recipe"

        # lint / format / typecheck tools — the tool name IS the object, so a
        # bare "prettier failed" becomes "prettier: failed" (the user's example).
        # Capture an explicit path target after it when present.
        m = re.search(
            r"\b(prettier|eslint|tsc|ruff|black|mypy|flake8|pylint|golangci-lint"
            r"|rustfmt|gofmt|stylelint|biome|clippy)\b",
            cmd,
        )
        if m:
            tool = m.group(1)
            # Append "on <file>" only when a clear filename-with-extension is an
            # argument — never a flag ("--check") or subcommand ("check").
            fm = re.search(r"(?<!\S)([\w.\-/]+\.[A-Za-z]{1,5})\b", cmd[m.end():])
            if fm:
                tgt = fm.group(1)
                base = tgt.rsplit("/", 1)[-1] if "/" in tgt else tgt
                return f"{tool} on {base}"
            return tool

        # Subjectless test runners (no explicit file target): name the suite so
        # the listener still gets an object. "npm test", bare "pytest", etc.
        if re.search(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b", cmd):
            return "the test suite"
        if re.search(r"\b(?:pytest|go\s+test|cargo\s+test|jest|vitest|tox)\b", cmd):
            return "the tests"
        if re.search(r"\bmake\b", cmd):
            return "make"

        return ""

    # Test-runner command signatures — used by _extract_bash to upgrade a bare
    # "test result" hint to "the test suite" when no specific target was named.
    _TEST_RUNNER_RE = re.compile(
        r"\b(pytest|tox|nox|jest|vitest|mocha|ava|rspec|phpunit|"
        r"go\s+test|cargo\s+test|gradle(?:w)?\s+test|mvn\s+test|"
        r"(?:npm|pnpm|yarn|bun|deno)\s+(?:run\s+)?test)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_test_command(cls, command: str) -> bool:
        """True if the command is a recognized test runner. Lets _extract_bash
        name "the test suite" as the object when no more-specific target was
        parsed, WITHOUT upgrading an unrecognized script ('./run_tests.sh') that
        must keep the bare 'test result' hint."""
        return bool(command) and bool(cls._TEST_RUNNER_RE.search(command))

    # Command-verb → spoken WHAT, for the build-output and generic-stdout
    # fallbacks when _bash_target_hint() found no explicit target. Each maps a
    # verb/keyword present in the command to a concise object the listener can
    # use ("install" -> "the install"). Ordered most-specific first; the first
    # hit wins. Returns '' when nothing is inferable (do NOT invent an object).
    _VERB_HINTS: tuple[tuple[str, str], ...] = (
        (r"\b(?:pip|pip3|npm|pnpm|yarn|bun|cargo|go|brew|apt|gem)\s+install\b", "the install"),
        (r"\binstall\b", "the install"),
        (r"\b(?:docker\s+)?build\b", "the build"),
        (r"\b(?:lint|eslint|ruff|flake8|pylint|golangci-lint|clippy)\b", "the lint"),
        (r"\blicense\w*\b", "the license check"),
        (r"\bmetric\w*\b", "the metrics"),
        (r"\bbranch\w*\b", "the branch"),
        (r"\bmigrat\w*\b", "the migration"),
        (r"\bdeploy\w*\b", "the deploy"),
        (r"\b(?:compile|compil\w*)\b", "the compile"),
    )

    @classmethod
    def _infer_command_what(cls, command: str) -> str:
        """Infer a spoken object from the command VERB when no explicit target
        was parsed by _bash_target_hint. Drives the build-output and generic
        stdout fallbacks so "bash output"/"build output" gain a real WHAT
        (install/build/lint/license/metrics/branch/...). Returns '' when nothing
        is inferable — the caller then leaves the generic hint as-is (no
        fabrication)."""
        if not command:
            return ""
        for pat, what in cls._VERB_HINTS:
            if re.search(pat, command, re.IGNORECASE):
                return what
        return ""

    @staticmethod
    def _grep_target_file(tool_input: dict) -> str:
        """The FILE/GLOB a grep searched, as a short spoken basename. Most grep
        calls scope to a path or glob; when the pattern is missing this names the
        WHAT so the listener hears 'grep in router.py' instead of bare 'grep
        result'. Empty string when no salient single target is present."""
        if not isinstance(tool_input, dict):
            return ""
        raw = tool_input.get("path") or tool_input.get("glob") or tool_input.get("file") or ""
        raw = str(raw).strip()
        if not raw or raw in (".", "./", "*", "**"):
            return ""
        # Speak the basename only (the deepest, most identifying segment).
        base = raw.rstrip("/").rsplit("/", 1)[-1]
        # A bare directory or a wildcard glob ("src", "**/*.py" -> "*.py") is not
        # a useful WHAT — only name a concrete file.
        if not base or "*" in base or base in ("src", "lib", "tests", "test"):
            return ""
        return base[:40]

    def _extract_grep(self, event: dict, stdout: str) -> tuple[str, str]:
        # Prefer a pre-counted field if the tool supplied one.
        tool_input = event.get("tool_input") or {}
        if isinstance(tool_input, dict):
            count_hint = tool_input.get("output_mode")  # informational
            pattern = (tool_input.get("pattern") or "")[:40]  # the search needle
        else:
            count_hint = None
            pattern = ""

        if not stdout:
            return "", ""

        # Try to count lines (matches) directly from stdout.
        nonblank = [ln for ln in stdout.splitlines() if ln.strip()]
        n = len(nonblank)
        if n == 0:
            return "", ""

        # Speak a single-line summary, target-prefixed when the bare count
        # is too generic and the search pattern is short enough to vocalize.
        noun = "match" if n == 1 else "matches"
        bare = f"{n} {noun}."
        # Verbosity check uses the search pattern as the "target" — what was
        # being searched for. e.g. "47 matches." + pattern="TODO" →
        # "47 matches for TODO." (more spoken-friendly than "In TODO: 47 matches").
        if pattern and self._verbosity_for(bare, pattern) == "targeted":
            content = f"{n} {noun} for {pattern!s}."
        else:
            content = bare
        # context_hint: name the OBJECT so model doesn't synthesize a vague
        # "the search". Prefer the pattern (what was searched FOR); else fall
        # back to the FILE that was searched IN (most grep calls scope a path).
        if pattern:
            hint = f"grep for {pattern}"
        else:
            target_file = self._grep_target_file(tool_input)
            if target_file:
                hint = f"grep in {target_file}"
            elif count_hint:
                hint = f"grep ({count_hint})"
            else:
                hint = "grep result"
        return content, hint

    def _extract_task(self, stdout: str) -> tuple[str, str]:
        if not stdout:
            return "", ""
        # Subagent's last sentence is usually the conclusion.
        # Sentence split is intentionally lossy — punctuation-driven.
        sentences = re.split(r"(?<=[.!?])\s+", stdout.strip())
        for s in reversed(sentences):
            s = s.strip()
            if 12 <= len(s) <= 400:
                return s, "subagent finding"
        return "", ""

    def _extract_webfetch(self, stdout: str) -> tuple[str, str]:
        if not stdout:
            return "", ""
        # Look for an HTML <title> first, then a markdown heading.
        m = re.search(r"<title[^>]*>(.+?)</title>", stdout, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip(), "page title"
        m = re.search(r"^\s*#\s+(.+)$", stdout, re.MULTILINE)
        if m:
            return m.group(1).strip(), "page heading"
        return "", ""

    def _extract_error_snippet(self, stdout: str) -> str:
        """Return the first line containing an error keyword (with trailing context)."""
        for line in stdout.splitlines():
            if ERROR_KEYWORDS_RE.search(line):
                return line.strip()
        # Fallback: first non-empty line.
        for line in stdout.splitlines():
            if line.strip():
                return line.strip()
        return stdout.strip()

    # ------- Drop filter --------------------------------------------------

    def _drop_check(self, content: str) -> Optional[str]:
        """Run the regex drop filter. Returns drop-reason or None."""
        if not content:
            return "empty content"
        # Code blocks and system reminders are pasted noise.
        if CODE_FENCE_RE.search(content):
            return "code-block content"
        if SYSTEM_REMINDER_RE.search(content):
            return "system-reminder content"
        # Boilerplate openers — but ONLY when the opener IS essentially the
        # whole message (pure filler like "Let me check."). RECALL FIX
        # 2026-06-19: a long answer that merely *opens* with "I'll…/Here's…/
        # This is…" carries a real finding in its body (shadow.log replay found
        # 6 substantive answers 872–4524 chars wrongly vetoed this way). If it's
        # long enough to be worth summarizing, it's long enough to not be filler.
        stripped = content.strip()
        if NOISE_PREFIX_RE.match(stripped) and len(stripped) < self.summarize_threshold:
            return "boilerplate prefix"
        # Pure file path dumps
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        if PATH_ONLY_RE.match(first_line) and len(content.strip().splitlines()) <= 2:
            return "file-path-only content"
        # Wave 2.5 tuning: file listings (`ls -la` output) — never speak.
        if FILE_LISTING_RE.search(content):
            return "file-listing content (ls -la)"
        # Wave 2.5 tuning: URL/path-list dumps (route tables, etc.).
        if _looks_like_path_list(content):
            return "path-list content"
        # Wave 2.5 tuning: git commit log lines (hex prefix or [branch hash]).
        if GIT_COMMIT_LINE_RE.search(content):
            return "git-commit-log content"
        # HOTFIX 2026-05-05: git diff --stat / --shortstat noise.
        if GIT_DIFF_STAT_RE.search(content):
            return "git-diff-stat content"
        # HOTFIX 2026-05-05: symbol runs (+++++, -----, etc.) — unspeakable.
        if SYMBOL_RUN_RE.search(content):
            return "symbol-run content (likely diff/banner)"
        # Dedupe on normalized hash
        h = _hash_content(content)
        if h in self._recent_hashes:
            return "duplicate of recent content"
        return None

    def _note_hash(
        self, content: str, session_id: str = "default", recency_key: str = ""
    ) -> None:
        if not content:
            return
        self._recent_hashes.append(_hash_content(content))
        # Per-session recently-spoken window (R-context). Keyed by recency_key
        # when provided (so the ambiguous branch can record the FULL stdout it
        # checked against), else by the spoken content itself. Lazily created so
        # we never accumulate state for sessions that never speak. session_id
        # defaults to "default" to keep legacy callers safe.
        key = recency_key or content
        if not key:
            return
        sid = session_id or "default"
        window = self._recent_spoken_by_session.get(sid)
        if window is None:
            window = deque(maxlen=RECENT_SPOKEN_PER_SESSION_LIMIT)
            self._recent_spoken_by_session[sid] = window
        window.append(_hash_content(key))

    def _was_recently_spoken(self, content: str, session_id: str = "default") -> bool:
        """True if this content hash is in the session's recently-spoken window.

        Pure read against per-session state; used to enrich the LLM judge's
        context (NOT a gate — the deterministic dedupe in _drop_check still
        governs hard drops). Distinct from _drop_check's global window so the
        judge sees a SESSION-LOCAL recency signal.
        """
        if not content:
            return False
        window = self._recent_spoken_by_session.get(session_id or "default")
        if not window:
            return False
        return _hash_content(content) in window

    @classmethod
    def _judge_context(cls, event: dict, recently_spoken: bool = False) -> str:
        """Build a COMPACT context phrase for the LLM SPEAK/SKIP judge.

        Pure, deterministic, separately testable: given an event (with
        tool_input.command and/or cwd) plus a recently-spoken flag, return a
        short phrase the judge can use to weigh relevance IN CONTEXT. Never a
        transcript — at most a couple of short clauses so the judge prompt stays
        small enough to fit the budget.

        Signals plumbed (all already present on the event, zero new parsing):
          * cmd target  — what was run (reuses _bash_target_hint/_infer_command_what)
          * project dir — basename of cwd (which project/module this is)
          * recency     — was the same output spoken moments ago in this session

        Returns "" when no signal is available (judge falls back to the bare
        tool-name prompt).
        """
        if not isinstance(event, dict):
            return ""
        parts: list[str] = []

        tool_input = event.get("tool_input") or {}
        cmd = (tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        if cmd:
            target = cls._bash_target_hint(cmd) or cls._infer_command_what(cmd)
            if target:
                parts.append(f"ran {target}")

        cwd = event.get("cwd") or ""
        if isinstance(cwd, str) and cwd:
            proj = cwd.rstrip("/").rsplit("/", 1)[-1]
            # Skip non-salient roots ("/", "tmp", "") — they tell the judge
            # nothing about which project the developer is focused on.
            if proj and proj not in ("tmp", "/", "."):
                parts.append(f"in {proj}")

        if recently_spoken:
            parts.append("similar output already spoken this session")

        return "; ".join(parts)

    # ------- Ollama plumbing --------------------------------------------

    async def _binary_llm_judge(
        self, stdout: str, tool_name: str, judge_context: str = ""
    ) -> bool:
        """Single SPEAK / SKIP token from Ollama. Defaults to SKIP on any failure.

        We piggy-back on OllamaSummarizer.summarize() with a special
        STATUS-shaped prompt; the summarizer's existing safety net
        (``asyncio.wait_for(timeout=1.0)``) covers latency.

        ``judge_context`` is a COMPACT phrase (built by _judge_context) naming
        the active work — what command ran, which project, whether similar output
        was just spoken. It lets the judge weigh relevance in context instead of
        a vacuum. Kept short so the larger prompt never blows the budget.
        """
        if self.provider is None:
            return False
        # The judge prompt-build + token-parse live in the provider
        # (LLMProvider.judge). ContentRouter owns only the outer time budget
        # (binary_judge_timeout_s, clamped > the provider's inner cap) and the
        # default-SKIP-on-failure guarantee.
        try:
            return await asyncio.wait_for(
                self.provider.judge(stdout, tool_name, judge_context),
                timeout=self.binary_judge_timeout_s,
            )
        except asyncio.TimeoutError:
            log.debug("binary_llm_judge: timeout — defaulting SKIP")
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("binary_llm_judge: exception — defaulting SKIP: %s", exc)
            return False

    async def _maybe_summarize(self, decision: RouterDecision) -> RouterDecision:
        """Summarize with a config-driven wrapper timeout (self.summarize_timeout_s,
        clamped > the summarizer inner cap). On timeout/failure the original
        content is returned, but it is still normalized to clean speech downstream
        at ProcessStage (R1), so a fallback never speaks raw markdown."""
        if self.provider is None:
            return replace(decision, needs_summarization=False)

        try:
            summarized = await asyncio.wait_for(
                self.provider.summarize(
                    decision.content, decision.category, decision.context_hint
                ),
                timeout=self.summarize_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning(
                "summarize: timeout (%s, %d chars) — using original",
                decision.category.value, len(decision.content),
            )
            return replace(decision, needs_summarization=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("summarize: exception — using original: %s", exc)
            return replace(decision, needs_summarization=False)

        if not summarized or not summarized.strip():
            return replace(decision, needs_summarization=False)
        return replace(decision, content=summarized.strip(), needs_summarization=False)

    # ------- Pressure / queue-manager glue ------------------------------

    def _pressure_for(self, event: dict) -> float:
        """Look up QueueManager pressure for this session, default 1.0."""
        if self.queue_manager is None:
            return 1.0
        try:
            session_id = event.get("session_id") or "default"
            value = self.queue_manager.get_pressure(session_id)
            return float(value) if value else 1.0
        except Exception as exc:  # noqa: BLE001
            log.debug("get_pressure failed: %s — defaulting 1.0", exc)
            return 1.0

    # ------- Decision constructors --------------------------------------

    def _make_decision(
        self,
        *,
        event: dict,
        should_speak: bool,
        category: Category,
        content: str,
        priority: int,
        context_hint: str = "",
        needs_summarization: Optional[bool] = None,
    ) -> RouterDecision:
        # ROUTER-01 / ROUTER-06: enforce "default silence" allowlist.
        # Categories not in _SPEAKABLE_CATEGORIES are silenced unconditionally,
        # even if the caller passed should_speak=True.  This ensures that new
        # Category values added to the enum are silenced until explicitly
        # added to _SPEAKABLE_CATEGORIES.
        if should_speak and category not in _SPEAKABLE_CATEGORIES:
            log.debug(
                "_make_decision: category %r not in _SPEAKABLE_CATEGORIES — forcing silence",
                category,
            )
            should_speak = False

        # Auto-decide summarization if caller didn't override.
        if needs_summarization is None:
            needs_summarization = len(content) >= self.summarize_threshold

        return RouterDecision(
            should_speak=should_speak,
            category=category,
            content=content,
            priority=priority,
            source_event_id=event.get("event_id") or "",
            classified_at=_now(),
            needs_summarization=needs_summarization,
            context_hint=context_hint,
            raw_excerpt=_truncate(content, 80),
        )

    def _silence(self, event: Any, reason: str) -> RouterDecision:
        """Return a should_speak=False decision with the drop-reason in raw_excerpt."""
        source_event_id = ""
        if isinstance(event, dict):
            source_event_id = event.get("event_id") or ""
        return RouterDecision(
            should_speak=False,
            category=Category.STATUS,  # arbitrary; ignored when should_speak=False
            content="",
            priority=PRIORITY_LOW,
            source_event_id=source_event_id,
            classified_at=_now(),
            needs_summarization=False,
            context_hint="",
            raw_excerpt=_truncate(reason, 200),
        )


# ============================================================================
# TurnBuffer (W2.C) — per-session batching of INSIGHT + STATUS items
# ============================================================================

class TurnBuffer:
    """Per-session batching layer for INSIGHT + STATUS items.

    Lifecycle:
        * One TurnBuffer per session_id (lazy-created by
          :py:meth:`ContentRouter.turn_buffer_for`).
        * The daemon's socket handler routes INSIGHT/STATUS RoutedItems to
          ``add()`` instead of submitting them to the pipeline directly.
        * Buffer flushes on (a) explicit ``flush()`` (Stop hook) or
          (b) ``idle_window_ms`` of inactivity.
        * On flush, the buffered batch is handed to ``flush_callback`` —
          which is wired by W2.A to the pipeline submission entry point
          (typically a thin adapter around ``pipeline_adapter.submit_*``).

    Bypass rules:
        * ERROR bypasses the buffer entirely (instant playback via
          ``QueueManager.submit_priority``).
        * FINAL_ANSWER bypasses the buffer (it's the per-turn capstone and
          should not be merged with mid-turn insights).
        * Both rules are enforced by the *caller* (W2.A's socket handler),
          not by this class. The buffer accepts whatever it's given so it
          can be unit-tested cleanly without coupling to category logic.

    Concurrency:
        * Uses a per-instance ``asyncio.Lock`` to serialize add/flush.
        * The idle timer is scheduled with ``loop.call_later`` against the
          loop running ``add()``; flush is then scheduled as a task on that
          same loop. Must be called from a thread running an asyncio loop.
    """

    def __init__(
        self,
        session_id: str,
        flush_callback: FlushCallback,
        idle_window_ms: int = 800,
    ) -> None:
        self.session_id = session_id
        self.flush_callback = flush_callback
        self.idle_window_ms = int(idle_window_ms)

        self._pending: list[RoutedItem] = []
        self._idle_timer_handle: Optional[asyncio.Handle] = None
        # Lock serializes add()/flush() — keeps snapshot+clear and timer
        # arming free of races.
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- API

    async def add(self, item: RoutedItem) -> None:
        """Append ``item`` and (re)arm the idle timer.

        Must be called from a thread running an asyncio loop. The arming
        is no-op if the loop isn't reachable for some reason — the buffer
        still records the item so a manual ``flush()`` (e.g. on Stop) will
        still drain it.
        """
        if item is None:
            return
        async with self._lock:
            self._pending.append(item)
            self._arm_idle_timer_locked()

    async def flush(self, reason: str = "manual") -> None:
        """Drain the buffer through ``flush_callback``. Idempotent on empty.

        Snapshots and clears ``_pending`` under the lock, then awaits the
        callback OUTSIDE the lock — so a slow callback doesn't block
        concurrent ``add()`` calls (they'll just buffer into the next batch).
        """
        async with self._lock:
            self._cancel_idle_timer_locked()
            if not self._pending:
                return
            snapshot = self._pending
            self._pending = []

        # Callback runs outside the lock. We log failures rather than
        # propagate — the caller (Stop hook handler) treats flush as
        # best-effort drain.
        try:
            await self.flush_callback(snapshot)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "TurnBuffer[%s]: flush_callback raised on %s flush of %d item(s): %s",
                self.session_id, reason, len(snapshot), exc,
            )

    @property
    def pending_count(self) -> int:
        """Number of items currently buffered. Lock-free read — best-effort."""
        return len(self._pending)

    @property
    def oldest_age_ms(self) -> float:
        """Age (ms) of the oldest pending item, or 0.0 if empty.

        Computed against ``time.time()``; uses ``RoutedItem.submitted_at``.
        """
        if not self._pending:
            return 0.0
        oldest = self._pending[0].submitted_at
        return max(0.0, (_now() - oldest) * 1000.0)

    # ------------------------------------------------------------- internals

    def _arm_idle_timer_locked(self) -> None:
        """Cancel any existing timer and schedule a new one.

        Must be called with ``self._lock`` held. If we cannot reach a
        running loop (e.g., called from a synchronous test path) we just
        skip arming — the data is still buffered and a manual flush()
        will drain it.
        """
        self._cancel_idle_timer_locked()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug(
                "TurnBuffer[%s]: no running loop for idle-timer arm; "
                "data still buffered for manual flush",
                self.session_id,
            )
            return

        delay_s = self.idle_window_ms / 1000.0
        self._idle_timer_handle = loop.call_later(
            delay_s, self._on_idle_timer_fired
        )

    def _cancel_idle_timer_locked(self) -> None:
        """Cancel any pending idle timer. Safe to call repeatedly."""
        if self._idle_timer_handle is not None:
            try:
                self._idle_timer_handle.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._idle_timer_handle = None

    def _on_idle_timer_fired(self) -> None:
        """Idle-timer callback — schedules an idle flush as a task.

        ``loop.call_later`` must call a sync callback; we kick the async
        flush via ``create_task``. The task swallows its own exceptions
        via ``flush()``'s safety net.
        """
        # Drop our handle reference so a concurrent add() doesn't try to
        # cancel an already-fired timer.
        self._idle_timer_handle = None
        try:
            _t = asyncio.get_running_loop().create_task(
                self.flush(reason="idle"),
                name=f"turn-buffer-idle-flush[{self.session_id}]",
            )
            # DAEMON-04: prevent GC'd task from swallowing exception silently
            _t.add_done_callback(
                lambda t: t.exception() if not t.cancelled() and t.done() else None
            )
        except RuntimeError:
            # No loop — shouldn't happen since call_later only runs on a loop,
            # but guard anyway. Item stays buffered for manual flush.
            log.debug(
                "TurnBuffer[%s]: idle timer fired without a running loop",
                self.session_id,
            )
