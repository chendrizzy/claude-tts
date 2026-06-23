"""Unified Ollama-based summarization layer for the TTS pipeline.

Wraps the existing sync `OllamaClient.generate_response` so callers can
`await` summaries with a hard timeout. On any failure (timeout, network,
missing model) it falls back to a deterministic rule-based shortener and
NEVER raises to the caller.

Created by Wave 1 Agent C of the TTS overhaul. Consumed by ContentRouter
(W1.B) for >120-char content and by QueueManager (W1.D) for batch
condensation under YELLOW/RED tier pressure.

Single unified prompt template — see the plan's "Ollama Prompt Redesign"
section. Every Ollama call is wrapped in `asyncio.wait_for(timeout=1.0)`.
Trailing 60s avg latency is exposed via `avg_latency_ms` so QueueManager
can disable condensation when the model is slow.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

from daemon.tts_types import Category, RoutedItem

try:
    # Shared markup->speech normalizer (R1). Used so the rule-based fallback
    # truncation spends its char budget on real speech, not raw markdown.
    from daemon.text_utils import normalize_for_speech
except Exception:  # pragma: no cover - degrade gracefully
    def normalize_for_speech(t: str) -> str:  # type: ignore
        return t or ""

if TYPE_CHECKING:
    from daemon.ollama_integration import OllamaClient


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt template — keep in sync with the plan's "Ollama Prompt Redesign".
# Indented with leading spaces inside the f-string body so the model sees
# the same formatting as documented.
# ---------------------------------------------------------------------------
_SUMMARIZE_PROMPT_TEMPLATE = """You are F.R.I.D.A.Y. Summarize this for TTS readout.

CATEGORY: {category}
CONTEXT: {context_hint}
CONTENT: {content}

Rules:
- Speak the substance directly. No "Here is", no "The output shows", no preamble.
- First person ("I found"), never "we" or "the user".
- ALWAYS name the OBJECT — WHAT the action was about: the file, module, test,
  check, command, or task. The listener has no screen; a bare outcome like
  "failed", "passed", or "3 errors" is useless without the thing it refers to.
  The actor is the agent on the active task — that is assumed, so do NOT narrate
  it ("the agent ran..."). Spend the words on the OBJECT instead. When the
  content omits the object, take it from CONTEXT above.
- Preserve every contraction exactly. Never expand "don't" to "do not".
- Strip code, file paths, identifiers unless they ARE the point.
- Length proportional to substance:
    one fact -> one sentence
    cause + effect -> two sentences
    complex finding -> three sentences max
- Dry, understated tone. No exclamation points.
- ERROR: name WHAT broke (the file/check/command), where, then why.
- STATUS: name the OBJECT that ran or was checked, THEN the outcome/number
  (e.g. "the router tests: 23 passed, 4 failed"; "prettier: failed on 3 files"
  — never a bare "failed" or "23 passed" with no object).
- INSIGHT: state the realization, then implication.
- FINAL_ANSWER: just the answer, no framing.

Speak it:
"""

_BATCH_PROMPT_TEMPLATE = (
    "Combine these {n} {category} updates into ONE spoken sentence "
    "(max 25 words). Preserve every distinct file name, error code, "
    "and number. Drop pleasantries. Output sentence only.\n\n"
    "{joined}\n\n"
    "Sentence:"
)


# Rule-based fallback constants
_FALLBACK_MAX_CHARS = 200
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
# Sentence-end break: ., !, or ? followed by whitespace.
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+")

# Latency tracking window (seconds).
_LATENCY_WINDOW_S = 60.0

# Ollama call tuning.
# R2: lowered 250 -> 120. model latency scales with OUTPUT token count, not
# input length; 250-token summaries took 2-3s (over the cap -> fallback). A TTS
# summary is "three sentences max" per the prompt, so ~120 tokens is ample and
# lands well under the inner cap, restoring real-summary completion.
_SUMMARIZE_MAX_TOKENS = 120
_BATCH_MAX_TOKENS = 80
_TEMPERATURE = 0.3  # Low for deterministic summaries.
_WARMUP_MAX_TOKENS = 4


def rule_based_summary(content: str) -> str:
    """Normalize markup to speech (R1), then truncate at the last sentence
    boundary within `_FALLBACK_MAX_CHARS`. Falls back to a hard char-cut if no
    sentence boundary is found.

    Normalizing BEFORE truncation means the budget is spent on real speech, not
    markdown cut mid-token. Module-level so the deterministic summary path is
    single-source — shared by OllamaSummarizer._rule_based_fallback and the
    no-LLM NullProvider.
    """
    text = normalize_for_speech(content)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= _FALLBACK_MAX_CHARS:
        return text

    head = text[:_FALLBACK_MAX_CHARS]
    # Find last sentence-end within head.
    last_break = -1
    for match in _SENTENCE_BREAK_RE.finditer(head):
        last_break = match.start()  # position of the . / ! / ?
    if last_break > 40:  # avoid keeping a single 3-word stub
        return head[: last_break + 1].strip()
    return head.rstrip() + "..."


class OllamaSummarizer:
    """Async wrapper around `OllamaClient.generate_response` with timeout +
    rule-based fallback. Maintains a 60s rolling latency window so
    QueueManager can detect a degraded model.
    """

    def __init__(
        self,
        ollama_client: "OllamaClient",
        model: str = "qwen2.5-coder:1.5b",
        timeout_s: float = 3.5,
        keep_alive: object = "30m",
        warm_interval_s: float = 120.0,
    ) -> None:
        self._client = ollama_client
        self._model = model
        # R2: inner cap raised 1.0s -> 3.5s AND _SUMMARIZE_MAX_TOKENS lowered
        # 250 -> 120. Live model latency scales with OUTPUT tokens: a 250-token
        # summary took 2-3s and blew the old caps; ~120 tokens lands ~1.5s, and
        # 3.5s leaves margin for outliers. The outer wrappers
        # (content_router._maybe_summarize, queue_manager._condense_or_fallback)
        # are clamped to always exceed this so a slow call returns the
        # markdown-clean fallback rather than raw content.
        self._timeout_s = timeout_s
        # keep_alive holds the model resident between bursts so the first call
        # after an idle gap doesn't pay cold-load latency; warm_interval_s is
        # the periodic re-warm cadence (kept < the keep_alive window).
        self._keep_alive = keep_alive
        self._warm_interval_s = warm_interval_s
        # Each entry: (completed_at_ts, latency_ms). Bound implicitly by window.
        self._latency_log: deque[tuple[float, float]] = deque()
        self._lock = asyncio.Lock()

    # ----- Public API ----------------------------------------------------
    async def summarize(
        self,
        content: str,
        category: Category,
        context_hint: str = "",
        allow_fallback: bool = True,
    ) -> Optional[str]:
        """Return a spoken-form summary. Never raises.

        On timeout or any Ollama failure: if ``allow_fallback`` (default), return
        a normalized, sentence-truncated version of ``content``; if False, return
        None. The binary SPEAK/SKIP judge passes allow_fallback=False so a
        timeout yields an honest None instead of truncated stdout that the judge
        would mis-parse as a non-SPEAK verdict (S4).
        """
        if not content or not content.strip():
            return ""

        prompt = _SUMMARIZE_PROMPT_TEMPLATE.format(
            category=category.value if isinstance(category, Category) else str(category),
            context_hint=context_hint or "(none)",
            content=content,
        )

        result = await self._call_ollama(prompt, max_tokens=_SUMMARIZE_MAX_TOKENS)
        if result is None:
            return self._rule_based_fallback(content) if allow_fallback else None
        return result

    async def condense_batch(self, items: list[RoutedItem]) -> str:
        """Merge multiple same-category items into one utterance.

        Used by QueueManager's YELLOW/RED tier coalescing. Falls back to
        '. '.join() truncated to 200 chars on any failure. Never raises.
        """
        if not items:
            return ""
        if len(items) == 1:
            return items[0].decision.content

        # Pull category from first item (caller should batch by category).
        first = items[0].decision
        category_value = (
            first.category.value
            if isinstance(first.category, Category)
            else str(first.category)
        )

        joined = "\n".join(
            f"- {item.decision.content}" for item in items if item.decision.content
        )

        prompt = _BATCH_PROMPT_TEMPLATE.format(
            n=len(items),
            category=category_value,
            joined=joined,
        )

        result = await self._call_ollama(prompt, max_tokens=_BATCH_MAX_TOKENS)
        if result is None:
            return self._batch_fallback(items)
        return result

    async def warmup(self) -> bool:
        """Pre-warm the model with a 1-token generation so the first real
        burst doesn't pay cold-start latency. Called once by tts_daemon
        startup. Returns True on success, False otherwise. Never raises.
        """
        try:
            # Use a small dedicated timeout — model load can take longer
            # than steady-state inference.
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.generate_response,
                    "ok",
                    self._model,
                    _WARMUP_MAX_TOKENS,
                    _TEMPERATURE,
                    self._keep_alive,
                ),
                timeout=max(self._timeout_s * 30.0, 30.0),
            )
            ok = bool(result)
            if ok:
                logger.info(
                    "OllamaSummarizer warmup OK for model %r", self._model
                )
            else:
                logger.warning(
                    "OllamaSummarizer warmup returned empty for model %r", self._model
                )
            return ok
        except asyncio.TimeoutError:
            logger.warning(
                "OllamaSummarizer warmup timed out for model %r", self._model
            )
            return False
        except Exception as exc:  # noqa: BLE001 — warmup must never raise
            logger.warning(
                "OllamaSummarizer warmup failed for model %r: %s",
                self._model,
                exc,
            )
            return False

    async def keep_warm_loop(self) -> None:
        """Re-warm the model forever so it never goes cold between bursts (R2).

        Runs an initial warmup, then re-warms every ``warm_interval_s``. With
        keep_alive set on every real call this is belt-and-suspenders: a one-shot
        warmup is insufficient because Ollama unloads the model after its
        keep_alive window of inactivity. Never raises (except on cancellation).
        """
        await self.warmup()
        while True:
            try:
                await asyncio.sleep(self._warm_interval_s)
                await self.warmup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("keep_warm_loop iteration failed: %s", exc)

    @property
    def avg_latency_ms(self) -> float:
        """Trailing 60s average latency in ms. 0.0 if no samples."""
        self._evict_stale_samples()
        if not self._latency_log:
            return 0.0
        total = sum(latency for _, latency in self._latency_log)
        return total / len(self._latency_log)

    # ----- Internal ------------------------------------------------------
    async def _call_ollama(self, prompt: str, *, max_tokens: int) -> Optional[str]:
        """Invoke the sync OllamaClient via a thread, with hard timeout.

        Returns the trimmed response on success; returns None on any failure
        (including timeout, network error, or missing model). Records
        latency on success only — failures don't pollute the moving avg.
        """
        start = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.generate_response,
                    prompt,
                    self._model,
                    max_tokens,
                    _TEMPERATURE,
                    self._keep_alive,
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self._log_warning(
                f"Ollama call timed out after {elapsed_ms:.0f}ms "
                f"(limit {self._timeout_s * 1000:.0f}ms); using fallback"
            )
            return None
        except Exception as exc:  # noqa: BLE001 — never propagate to caller
            self._log_warning(f"Ollama call failed: {exc!r}; using fallback")
            return None

        if raw is None:
            self._log_warning("Ollama returned None; using fallback")
            return None

        cleaned = raw.strip()
        if not cleaned:
            self._log_warning("Ollama returned empty string; using fallback")
            return None

        # Record latency only on success.
        elapsed_ms = (time.monotonic() - start) * 1000.0
        self._record_latency(elapsed_ms)
        return cleaned

    def _record_latency(self, latency_ms: float) -> None:
        now = time.monotonic()
        self._latency_log.append((now, latency_ms))
        self._evict_stale_samples(now)

    def _evict_stale_samples(self, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic()
        cutoff = now - _LATENCY_WINDOW_S
        while self._latency_log and self._latency_log[0][0] < cutoff:
            self._latency_log.popleft()

    @staticmethod
    def _rule_based_fallback(content: str) -> str:
        """Delegates to the module-level rule_based_summary (shared with the
        no-LLM NullProvider so the deterministic summary path is single-source)."""
        return rule_based_summary(content)

    @classmethod
    def _batch_fallback(cls, items: list[RoutedItem]) -> str:
        """Join item contents with '. ' and apply the same rule-based
        truncation as `_rule_based_fallback`.
        """
        joined = ". ".join(
            item.decision.content.rstrip(".")
            for item in items
            if item.decision.content
        )
        return cls._rule_based_fallback(joined)

    @staticmethod
    def _log_warning(message: str) -> None:
        """Log to module logger AND stderr — daemon log routing may not
        be configured when this fires, and we want fallback events visible
        during early startup.
        """
        logger.warning(message)
        print(f"[ollama_summarizer] {message}", file=sys.stderr)
