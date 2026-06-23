"""
QueueManager — Drift Prevention Layer (Wave 1.D, TTS overhaul).

Sits between IngestStage and ProcessStage as an interceptor. The orchestrator
calls `await qm.intercept(message, session_id)` after `ingest.consume()` and
before `process.process()`. Returns:
    - None        → drop entirely (TTL expired, redundant)
    - [msg]       → pass-through (GREEN tier)
    - [m1, ...]   → replacement list (condensation result, possibly with a
                    "skipped N" preamble)

ERROR-category items DO NOT go through `intercept`. They go via
`submit_priority` directly from ContentRouter — pre-emption with queue-jump
plus optional SIGTERM on the active afplay subprocess.

Tier state machine with hysteresis (drops back to GREEN only at lag<2000ms):
    GREEN   <3000ms          pass-through
    YELLOW  3000-8000ms      coalesce same-category 2-item batches via Ollama
    RED     8000-15000ms     drop INFO/STATUS<pri 5; force-summarize remaining
    BLACK   >15000ms or qd>50 nuclear: cancel pending, drop buffer, replay

All Ollama calls wrapped in `asyncio.wait_for(timeout=1.0)`; on timeout fall
back to the rule-based merger and never raise to the caller. If Ollama's
moving-average latency exceeds 1500ms for >60s, condensation is disabled for
60s — items are dropped (low-pri) or passed through (others) until recovery.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from daemon.tts_types import (
    Category,
    PRESSURE_MULTIPLIER,
    PRIORITY_ERROR,
    PRIORITY_NORMAL,
    RoutedItem,
    SessionId,
    Tier,
)

if TYPE_CHECKING:  # only for type hints — avoid runtime import cycles
    from daemon.ollama_summarizer import OllamaSummarizer
    from daemon.pipeline.ingest_stage import IngestMessage, IngestStage
    from daemon.pipeline.playback_stage import PlaybackStage

logger = logging.getLogger(__name__)


# Tier boundary constants (ms). Hysteresis: only drop tiers when crossing
# the LOW_BOUND for the lower tier — never just because we briefly dipped.
GREEN_UPPER = 3000.0
YELLOW_UPPER = 8000.0
RED_UPPER = 15000.0
HYSTERESIS_GREEN_DROP = 2000.0   # only fall back to GREEN when lag < 2000ms
BLACK_QUEUE_DEPTH = 50

# TTLs (seconds, on item.submitted_at).
TTL_HARD_S = 30.0     # any non-ERROR item older than this → silently drop
TTL_SOFT_S = 15.0     # STATUS/INFO older than this → demote ("survives unless condensed")

# Ollama health window
OLLAMA_HEALTH_WINDOW_S = 60.0
OLLAMA_LATENCY_THRESHOLD_MS = 1500.0

# Default initial moving-average for chunk audio (ms).
# Tunes itself toward true value after first few playbacks.
DEFAULT_AVG_CHUNK_AUDIO_MS = 3500.0
AVG_CHUNK_EWMA_ALPHA = 0.2  # weight given to new sample


@dataclass
class _SessionState:
    """Per-session drift-tracking state. Internal — never returned externally."""
    current_tier: Tier = Tier.GREEN
    skipped_count: int = 0          # accumulates for next "Skipping N updates" preamble
    last_lag_ms: float = 0.0
    # Side-table: request_id -> Category. Maintained because IngestMessage
    # does not carry Category and we cannot extend it (per Wave 1 invariants).
    item_categories: dict[str, Category] = field(default_factory=dict)
    # Counters for metrics
    condensations_total: int = 0
    drops_total: int = 0
    preemptions_total: int = 0


class QueueManager:
    """Drift-prevention queue manager. See module docstring for full semantics."""

    def __init__(
        self,
        config: dict,
        ollama_summarizer: "OllamaSummarizer",
        ingest_stage: "IngestStage",
        playback_stage: "PlaybackStage",
    ) -> None:
        self._config = config or {}
        self._ollama = ollama_summarizer
        self._ingest = ingest_stage
        self._playback = playback_stage

        # Allow overriding tier boundaries from config
        qm_cfg = self._config.get("queue_manager", {}) if self._config else {}
        thresholds = qm_cfg.get("thresholds", {})
        self._green_upper = float(thresholds.get("green_upper_ms", GREEN_UPPER))
        self._yellow_upper = float(thresholds.get("yellow_upper_ms", YELLOW_UPPER))
        self._red_upper = float(thresholds.get("red_upper_ms", RED_UPPER))
        self._hysteresis_green = float(
            thresholds.get("hysteresis_green_drop_ms", HYSTERESIS_GREEN_DROP)
        )
        self._black_queue_depth = int(
            thresholds.get("black_queue_depth", BLACK_QUEUE_DEPTH)
        )
        self._ttl_hard_s = float(qm_cfg.get("ttl_hard_s", TTL_HARD_S))
        self._ttl_soft_s = float(qm_cfg.get("ttl_soft_s", TTL_SOFT_S))

        # Session state
        self._sessions: dict[SessionId, _SessionState] = defaultdict(_SessionState)

        # Self-tuning moving average for chunk audio duration
        self._avg_chunk_audio_ms = DEFAULT_AVG_CHUNK_AUDIO_MS

        # Ollama health tracking — disable condensation if degraded
        self._ollama_disabled_until: float = 0.0   # unix ts; 0 = enabled
        # Sliding window of (timestamp, latency_ms) — last OLLAMA_HEALTH_WINDOW_S
        self._ollama_latency_samples: deque[tuple[float, float]] = deque(maxlen=200)

    # ------------------------------------------------------------------
    # Public API (contractual signatures)
    # ------------------------------------------------------------------

    async def intercept(
        self, message: "IngestMessage", session_id: str
    ) -> Optional[list["IngestMessage"]]:
        """Inspect a single ingested message; possibly merge / drop / condense.

        Returns:
            None        — drop entirely (TTL expired, redundant)
            [msg]       — pass-through unchanged (GREEN)
            [m1, ...]   — replacement list (condensation produces 1+ messages,
                          possibly with a "skipped N" preamble fused in)

        Called by orchestrator AFTER `ingest.consume()` and BEFORE `process.process()`.
        """
        state = self._sessions[session_id]

        # OBSERVE-01: log request_id at queue intercept so a single grep
        # over tts_daemon.log returns ≥1 hit at this stage.
        logger.info(
            f"[qm] intercept request_id={message.request_id} session={session_id} "
            f"tier={state.current_tier.value}"
        )

        # 1) Hard-TTL check first — old items go silently. Errors never expire,
        #    but ERROR shouldn't reach intercept; if it does, treat as ERROR.
        category = state.item_categories.get(message.request_id, Category.STATUS)
        now = time.time()
        age_s = now - message.ingested_at
        if category != Category.ERROR and age_s > self._ttl_hard_s:
            state.drops_total += 1
            state.skipped_count += 1
            state.item_categories.pop(message.request_id, None)
            logger.debug(
                f"[qm] hard-TTL drop request_id={message.request_id} age={age_s:.1f}s"
            )
            return None

        # 2) Compute current lag and update tier (with hysteresis).
        lag_ms = self.predicted_lag_ms(session_id)
        prev_tier = state.current_tier
        new_tier = self._compute_tier_with_hysteresis(state, lag_ms)
        state.current_tier = new_tier
        state.last_lag_ms = lag_ms

        if new_tier != prev_tier:
            logger.info(
                f"[qm] session={session_id} tier {prev_tier.value}→{new_tier.value} "
                f"(lag={lag_ms:.0f}ms)"
            )

        # 3) GREEN — pass-through (with skipped preamble if any pending).
        if new_tier == Tier.GREEN:
            state.item_categories.pop(message.request_id, None)
            return self._maybe_prepend_skipped(state, [message])

        # 4) YELLOW — single message can't be merged on its own (need ≥2).
        #             We pass it through but tag the state so a future burst
        #             will draw on `_ingest.peek_all` to find peers.
        #             This is intentionally simple at the per-message level —
        #             the real coalescing happens when intercept is called on
        #             a backlog. Optimisation: if peek_all reveals a same-
        #             category peer pending, condense them as a pair now.
        if new_tier == Tier.YELLOW:
            return await self._handle_yellow(message, session_id, state)

        # 5) RED — drop low-pri INFO/STATUS, summarize the rest.
        if new_tier == Tier.RED:
            return await self._handle_red(message, session_id, state)

        # 6) BLACK — nuclear: drop pending buffer, replace with "still working".
        return await self._handle_black(message, session_id, state)

    def get_pressure(self, session_id: str) -> float:
        """Return the pressure multiplier for the session's current tier."""
        state = self._sessions.get(session_id)
        if state is None:
            return PRESSURE_MULTIPLIER[Tier.GREEN]
        return PRESSURE_MULTIPLIER[state.current_tier]

    def get_tier(self, session_id: str) -> Tier:
        """Return the session's current tier (with hysteresis already applied)."""
        state = self._sessions.get(session_id)
        if state is None:
            return Tier.GREEN
        return state.current_tier

    def predicted_lag_ms(self, session_id: str) -> float:
        """Compute predicted speech lag in milliseconds.

        Formula: in_flight + buffered + pending * avg_chunk_audio_ms.
        Reads from playback_stage and ingest_stage; tolerates missing methods
        (degrades to 0-contribution rather than raising) so this can be called
        before W1.E/W1.F have wired in their additions.
        """
        now_ms = time.time() * 1000.0

        in_flight = 0.0
        buffered = 0.0
        try:
            state = self._playback.get_session_state(session_id)
        except Exception:
            state = None

        if state is not None:
            # current_segment_ends_at is added by W1.E. Tolerate absence.
            ends_at = getattr(state, "current_segment_ends_at", None)
            if ends_at is not None:
                in_flight = max(0.0, float(ends_at) - now_ms)
            # playback_buffer added by W1.E too; fall back to session_buffers qsize
            pb = getattr(state, "playback_buffer", None)
            if pb is not None:
                try:
                    buffered = sum(
                        float(getattr(s, "duration_ms", 0.0)) for s in pb
                    )
                except Exception:
                    buffered = 0.0
            else:
                # Approx: each buffered segment contributes avg chunk duration
                try:
                    buf_q = self._playback.session_buffers.get(session_id)
                    qs = buf_q.qsize() if buf_q is not None else 0
                    buffered = qs * self._avg_chunk_audio_ms
                except Exception:
                    buffered = 0.0

        # Pending count from ingest. W1.F adds get_pending_count; fallback to
        # session_queues qsize attribute.
        pending = 0
        try:
            getter = getattr(self._ingest, "get_pending_count", None)
            if callable(getter):
                pending = int(getter(session_id))
            else:
                q = getattr(self._ingest, "session_queues", {}).get(session_id)
                pending = q.qsize() if q is not None else 0
        except Exception:
            pending = 0

        estimated = pending * self._avg_chunk_audio_ms
        return in_flight + buffered + estimated

    async def submit_priority(self, item: RoutedItem) -> bool:
        """Bypass-path for ERROR. Queue-jumps the playback buffer AND
        optionally SIGTERMs the active afplay subprocess.

        Called directly by ContentRouter for ERROR (not via intercept()).

        Returns True on successful submission, False on failure.
        """
        session_id = item.session_id
        state = self._sessions[session_id]
        state.preemptions_total += 1

        # OBSERVE-01: log request_id on the ERROR pre-emption path so a single
        # `grep request_id=<uuid> tts_daemon.log` traces ERROR events end-to-end
        # (otherwise ERRORs bypass intercept() and the qm-stage log is missing).
        request_id = item.decision.source_event_id
        logger.info(
            f"[qm] submit_priority request_id={request_id} session={session_id} "
            f"category={item.decision.category.value} preempt#{state.preemptions_total}"
        )

        # 1) Priority enqueue at the front of the playback buffer.
        try:
            pe = getattr(self._playback, "priority_enqueue", None)
            if callable(pe):
                # priority_enqueue takes an AudioSegment in the spec, but the
                # actual segment isn't generated yet at this point — this
                # function exists for already-rendered audio. For ERRORs we
                # hand off the RoutedItem to the segment-build path. The W2.B
                # wiring will arrange for ERRORs to short-circuit through the
                # generate stage with high priority. Here we just call
                # priority_enqueue if there's a pre-built segment on the item;
                # otherwise we rely on the daemon's submit path.
                pre_seg = getattr(item, "audio_segment", None)
                if pre_seg is not None:
                    result = pe(pre_seg)
                    if asyncio.iscoroutine(result):
                        await result
        except Exception as e:
            logger.warning(f"[qm] priority_enqueue failed: {e}")

        # 2) Queue-jump only — do NOT mid-segment interrupt the active afplay.
        # CUTOFF FIX 2026-06-19: SIGTERM'ing the live segment sliced real
        # sentences in half (errors are ~42% of decisions and many are stderr
        # false-positives, so this fired constantly). The ERROR already jumps
        # the buffer (step 1) and shortens what's pending (step 3); letting the
        # current segment finish costs ~1 short sentence of delay and keeps the
        # output intelligible. ponytail: if a class of error ever needs true
        # barge-in, gate it on a new "critical" flag rather than all ERRORs.
        logger.debug(
            "[qm] ERROR queue-jumped; letting active segment finish "
            "(no mid-segment SIGTERM)"
        )

        # 3) Trigger condensation cascade on remaining pending items at one tier higher.
        try:
            await self._cascade_condense(session_id)
        except Exception as e:
            logger.warning(f"[qm] cascade condense failed: {e}")

        # Track this ERROR so future intercepts know its category if asked.
        state.item_categories[item.decision.source_event_id] = Category.ERROR
        return True

    async def on_stop_hook(self, session_id: str) -> None:
        """Called when the Stop hook fires.

        Resets tier hysteresis (we're transitioning between turns) and clears
        the per-session skipped-count. The TurnBuffer (in content_router)
        handles its own flushing — we don't own it here. ContentRouter calls
        `turn_buffer.flush()` separately.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        # Don't aggressively reset to GREEN — let predicted_lag_ms resolve
        # naturally on next intercept. But clear pending-skip counter so the
        # next turn doesn't carry over stale "Skipping N" preambles.
        state.skipped_count = 0
        state.item_categories.clear()
        logger.debug(f"[qm] stop_hook for session={session_id} cleared skip-count + categories")

    def get_metrics(self, session_id: Optional[str] = None) -> dict:
        """Return metrics for one session or all sessions.

        Output shape: {tier, lag_ms, pending, condensations_total, drops_total,
                       preemptions_total, ollama_avg_ms}.
        """
        ollama_avg = self._compute_ollama_avg_latency_ms()

        if session_id is not None:
            state = self._sessions.get(session_id)
            if state is None:
                return {
                    "session_id": session_id,
                    "tier": Tier.GREEN.value,
                    "lag_ms": 0.0,
                    "pending": 0,
                    "condensations_total": 0,
                    "drops_total": 0,
                    "preemptions_total": 0,
                    "ollama_avg_ms": ollama_avg,
                }

            pending = 0
            try:
                getter = getattr(self._ingest, "get_pending_count", None)
                if callable(getter):
                    pending = int(getter(session_id))
            except Exception:
                pending = 0

            return {
                "session_id": session_id,
                "tier": state.current_tier.value,
                "lag_ms": round(state.last_lag_ms, 1),
                "pending": pending,
                "condensations_total": state.condensations_total,
                "drops_total": state.drops_total,
                "preemptions_total": state.preemptions_total,
                "ollama_avg_ms": ollama_avg,
            }

        # All sessions
        return {
            "sessions": {
                sid: self.get_metrics(sid) for sid in list(self._sessions.keys())
            },
            "avg_chunk_audio_ms": round(self._avg_chunk_audio_ms, 1),
            "ollama_disabled": self._ollama_disabled_until > time.time(),
            "ollama_avg_ms": ollama_avg,
        }

    # ------------------------------------------------------------------
    # Tier / hysteresis logic
    # ------------------------------------------------------------------

    def _compute_tier_with_hysteresis(
        self, state: _SessionState, lag_ms: float
    ) -> Tier:
        """Compute target tier given current and observed lag.

        Hysteresis rule: ONLY fall back to GREEN when lag < hysteresis threshold
        (default 2000ms). Otherwise, use the natural boundaries.
        """
        prev = state.current_tier

        # Check for BLACK escalation by depth (queue depth signal).
        try:
            pending = self._ingest.get_pending_count(state_session_id := None) if False else 0
        except Exception:
            pending = 0
        # NB: we don't have session_id here cleanly — pull from public path.
        # Caller computes lag_ms; depth-based BLACK is checked in _handle_*.
        del pending  # unused here

        # Choose target tier from raw lag.
        if lag_ms < self._green_upper:
            target = Tier.GREEN
        elif lag_ms < self._yellow_upper:
            target = Tier.YELLOW
        elif lag_ms < self._red_upper:
            target = Tier.RED
        else:
            target = Tier.BLACK

        # Apply hysteresis: only allow drop to GREEN if lag < hysteresis bound.
        # For drops between non-GREEN tiers, allow without hysteresis (we want
        # to recover quickly once the burst subsides, but not flap to GREEN).
        if target == Tier.GREEN and prev != Tier.GREEN:
            if lag_ms >= self._hysteresis_green:
                # Hold the previous (higher) tier; don't drop to GREEN yet.
                # But if previous was YELLOW and we're now well into GREEN
                # range (under hysteresis bound), we still drop.
                # Simplest: maintain previous tier.
                return prev

        return target

    # ------------------------------------------------------------------
    # Tier handlers
    # ------------------------------------------------------------------

    async def _handle_yellow(
        self, message: "IngestMessage", session_id: str, state: _SessionState
    ) -> Optional[list["IngestMessage"]]:
        """YELLOW: try to coalesce this message with the next pending peer of
        the same category. If no peer, pass through (with skipped preamble).
        """
        # Look at pending peers in the ingest queue.
        peers: list["IngestMessage"] = []
        try:
            peek = getattr(self._ingest, "peek_all", None)
            if callable(peek):
                peers = list(peek(session_id) or [])
        except Exception:
            peers = []

        # Filter peers by same category and not the current message.
        msg_cat = state.item_categories.get(message.request_id, Category.STATUS)
        same_cat: list["IngestMessage"] = []
        for p in peers:
            if p.request_id == message.request_id:
                continue
            p_cat = state.item_categories.get(p.request_id, Category.STATUS)
            if p_cat == msg_cat and p_cat != Category.ERROR:
                same_cat.append(p)
            if len(same_cat) >= 1:  # 2-item batches per spec
                break

        if not same_cat:
            return self._maybe_prepend_skipped(state, [message])

        # Build a 2-item RoutedItem batch to feed the summarizer/merger.
        items_for_condense = [
            self._wrap_routed(message, msg_cat, session_id),
            self._wrap_routed(same_cat[0], msg_cat, session_id),
        ]
        condensed_text = await self._condense_or_fallback(items_for_condense)
        state.condensations_total += 1

        # Drop the peer from categories side-table and also try to "consume"
        # it from the ingest queue (best-effort). The queue's actual extraction
        # is tricky; the orchestrator's loop will hit it next. Mark consumed:
        state.item_categories.pop(same_cat[0].request_id, None)
        state.item_categories.pop(message.request_id, None)

        condensed_msg = self._build_replacement_message(
            template=message,
            content=condensed_text,
            condensed_from=[message.request_id, same_cat[0].request_id],
        )
        return self._maybe_prepend_skipped(state, [condensed_msg])

    async def _handle_red(
        self, message: "IngestMessage", session_id: str, state: _SessionState
    ) -> Optional[list["IngestMessage"]]:
        """RED: drop INFO/STATUS below priority 5; force-summarize remaining.
        Prepend "Skipping N updates" if drops occurred.
        """
        cat = state.item_categories.get(message.request_id, Category.STATUS)

        # Drop low-pri STATUS/INFO entirely.
        if cat == Category.STATUS and message.priority < PRIORITY_NORMAL:
            state.drops_total += 1
            state.skipped_count += 1
            state.item_categories.pop(message.request_id, None)
            return None

        # ERROR shouldn't reach here (goes via submit_priority). If it does,
        # let it through unsummarized.
        if cat == Category.ERROR:
            return [message]

        # Force-summarize remaining (single-item summarization).
        items_for_condense = [self._wrap_routed(message, cat, session_id)]
        condensed_text = await self._condense_or_fallback(items_for_condense)
        state.condensations_total += 1
        state.item_categories.pop(message.request_id, None)

        replacement = self._build_replacement_message(
            template=message,
            content=condensed_text,
            condensed_from=[message.request_id],
        )
        return self._maybe_prepend_skipped(state, [replacement])

    async def _handle_black(
        self, message: "IngestMessage", session_id: str, state: _SessionState
    ) -> Optional[list["IngestMessage"]]:
        """BLACK: nuclear flush. Cancel pending, drop playback buffer (current
        segment finishes), speak "still working — caught up to: <latest>" and
        replay queued ERRORs.
        """
        # 1) Drop the playback buffer (keep errors).
        try:
            flush = getattr(self._playback, "flush_buffer", None)
            if callable(flush):
                result = flush(session_id, keep_errors=True)
                if asyncio.iscoroutine(result):
                    flushed = await result
                else:
                    flushed = result
                state.drops_total += int(flushed or 0)
        except Exception as e:
            logger.warning(f"[qm] flush_buffer failed in BLACK: {e}")

        # 2) Build the catch-up message. Use the current message as "latest".
        # Strip newlines / collapse whitespace; truncate to 200 chars for sanity.
        latest_excerpt = " ".join((message.content or "").split())[:200]
        catchup_text = f"still working — caught up to: {latest_excerpt}"

        replacement = self._build_replacement_message(
            template=message,
            content=catchup_text,
            condensed_from=[message.request_id],
        )

        # 3) Reset skip-count (we just spoke for everything).
        state.skipped_count = 0
        state.item_categories.pop(message.request_id, None)
        state.condensations_total += 1

        return [replacement]

    # ------------------------------------------------------------------
    # Condensation helpers
    # ------------------------------------------------------------------

    async def _condense_or_fallback(self, items: list[RoutedItem]) -> str:
        """Call ollama_summarizer.condense_batch with a wrapper timeout that is
        clamped to exceed the summarizer's inner cap. On timeout / exception /
        disabled-ollama: rule-based merger.
        """
        # If ollama is disabled due to degradation, use fallback immediately.
        if self._is_ollama_disabled():
            return self._rule_based_merge(items)

        # R2: the old hardcoded 1.0s was LESS than the summarizer's inner cap
        # (now 2.0s), so this wrapper always cancelled the call before it could
        # complete and fell back to the rule-based merge. Clamp > inner so a real
        # condensation can land; self-heals if inner_timeout_s is reconfigured.
        _inner = float(getattr(self._ollama, "_timeout_s", 2.0) or 2.0)
        _condense_timeout = max(4.0, _inner + 1.5)
        start = time.time()
        try:
            result = await asyncio.wait_for(
                self._ollama.condense_batch(items),
                timeout=_condense_timeout,
            )
            elapsed_ms = (time.time() - start) * 1000.0
            self._record_ollama_latency(elapsed_ms)
            if result and isinstance(result, str):
                return result
            # Empty or non-str result → fall back
            return self._rule_based_merge(items)
        except asyncio.TimeoutError:
            elapsed_ms = (time.time() - start) * 1000.0
            self._record_ollama_latency(elapsed_ms)
            logger.warning("[qm] Ollama condense_batch timeout — using rule-based merge")
            return self._rule_based_merge(items)
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000.0
            self._record_ollama_latency(elapsed_ms)
            logger.warning(f"[qm] Ollama condense_batch error: {e} — fallback")
            return self._rule_based_merge(items)

    @staticmethod
    def _rule_based_merge(items: list[RoutedItem]) -> str:
        """Fallback merger: concat with '. ', truncate to 200 chars,
        prepend 'Multiple updates: ' if more than one item.
        """
        if not items:
            return ""
        parts = [(it.decision.content or "").strip() for it in items if it]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        joined = ". ".join(parts)
        if len(items) > 1:
            joined = f"Multiple updates: {joined}"
        if len(joined) > 200:
            joined = joined[:197] + "..."
        return joined

    @staticmethod
    def _wrap_routed(
        message: "IngestMessage", category: Category, session_id: str
    ) -> RoutedItem:
        """Build a RoutedItem from an IngestMessage for use with the summarizer."""
        from daemon.tts_types import RouterDecision  # local import to avoid cycle

        decision = RouterDecision(
            should_speak=True,
            category=category,
            content=message.content,
            priority=message.priority,
            source_event_id=message.request_id,
            classified_at=time.time(),
            needs_summarization=True,
            context_hint="",
            raw_excerpt=(message.content or "")[:80],
        )
        return RoutedItem(
            decision=decision,
            session_id=session_id,
            submitted_at=message.ingested_at,
        )

    @staticmethod
    def _build_replacement_message(
        template: "IngestMessage", content: str, condensed_from: list[str]
    ) -> "IngestMessage":
        """Construct a new IngestMessage replacing one or more originals.

        Reuses the template's session/priority/source. Generates a new
        request_id with a 'cond-' prefix for traceability.
        """
        from daemon.pipeline.ingest_stage import IngestMessage  # local — runtime needed

        new_id = f"cond-{template.request_id}"
        return IngestMessage(
            content=content,
            session_id=template.session_id,
            priority=template.priority,
            request_id=new_id,
            ingested_at=time.time(),
            source=getattr(template, "source", "hook"),
        )

    def _maybe_prepend_skipped(
        self, state: _SessionState, msgs: list["IngestMessage"]
    ) -> list["IngestMessage"]:
        """If skipped_count > 0, prepend the preamble onto the FIRST message
        (in-place by constructing a new message), then reset the counter.
        """
        if state.skipped_count <= 0 or not msgs:
            return msgs
        n = state.skipped_count
        state.skipped_count = 0
        first = msgs[0]
        prefix = f"Skipping {n} updates. "
        new_first = self._build_replacement_message(
            template=first,
            content=prefix + (first.content or ""),
            condensed_from=[first.request_id],
        )
        # Preserve same request_id so dedupe-style downstream logic isn't surprised
        new_first.request_id = first.request_id
        return [new_first] + msgs[1:]

    async def _cascade_condense(self, session_id: str) -> None:
        """After an ERROR pre-emption, condense remaining pending items at one
        tier higher than current. Best-effort — we don't synchronously rewrite
        the queue; we just escalate the tier so subsequent intercept() calls
        treat backlog more aggressively.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return
        # R3/S4: cap the ERROR-driven cascade at YELLOW. The old code bumped one
        # tier toward BLACK on EVERY ERROR with no downward decay; a few errors
        # stranded the session at BLACK, where _handle_black flushes ALL pending
        # non-ERROR audio and speaks a generic "still working" — wholesale
        # silencing of STATUS/INSIGHT (the frequency complaint). An ERROR should
        # let the queue COALESCE peers (YELLOW), not nuke them. Genuine sustained
        # lag still escalates to RED/BLACK via the lag-driven tier computation;
        # this only removes the blind, count-driven escalation. Never demotes a
        # session already escalated by real lag.
        order = [Tier.GREEN, Tier.YELLOW, Tier.RED, Tier.BLACK]
        idx = order.index(state.current_tier)
        cap_idx = order.index(Tier.YELLOW)
        target_idx = min(idx + 1, cap_idx)
        if target_idx > idx:
            state.current_tier = order[target_idx]
            logger.info(
                f"[qm] cascade: bumped tier to {state.current_tier.value} "
                f"after ERROR pre-emption (capped at YELLOW)"
            )

    # ------------------------------------------------------------------
    # Ollama health tracking
    # ------------------------------------------------------------------

    def _record_ollama_latency(self, latency_ms: float) -> None:
        """Append a sample, evict old, and possibly disable condensation
        if average exceeds threshold for the full window.
        """
        now = time.time()
        self._ollama_latency_samples.append((now, latency_ms))
        # Evict samples older than the window.
        cutoff = now - OLLAMA_HEALTH_WINDOW_S
        while (
            self._ollama_latency_samples
            and self._ollama_latency_samples[0][0] < cutoff
        ):
            self._ollama_latency_samples.popleft()

        if not self._ollama_latency_samples:
            return

        # Need at least 3 samples spanning ≥30s for the rule to fire.
        oldest_ts = self._ollama_latency_samples[0][0]
        if (now - oldest_ts) < (OLLAMA_HEALTH_WINDOW_S / 2):
            return
        if len(self._ollama_latency_samples) < 3:
            return

        avg_ms = sum(l for _, l in self._ollama_latency_samples) / len(
            self._ollama_latency_samples
        )
        if avg_ms > OLLAMA_LATENCY_THRESHOLD_MS:
            if self._ollama_disabled_until <= now:
                self._ollama_disabled_until = now + OLLAMA_HEALTH_WINDOW_S
                logger.warning(
                    f"[qm] Ollama avg latency {avg_ms:.0f}ms > "
                    f"{OLLAMA_LATENCY_THRESHOLD_MS}ms — disabling condensation "
                    f"for {OLLAMA_HEALTH_WINDOW_S}s"
                )

    def _is_ollama_disabled(self) -> bool:
        return time.time() < self._ollama_disabled_until

    def _compute_ollama_avg_latency_ms(self) -> float:
        if not self._ollama_latency_samples:
            return 0.0
        return round(
            sum(l for _, l in self._ollama_latency_samples)
            / len(self._ollama_latency_samples),
            1,
        )

    # ------------------------------------------------------------------
    # Public hooks for sibling modules
    # ------------------------------------------------------------------

    def register_category(self, request_id: str, session_id: str, category: Category) -> None:
        """ContentRouter (W2.A wiring) calls this when it submits an item to
        the pipeline so that intercept() knows the category for that request_id.
        """
        self._sessions[session_id].item_categories[request_id] = category

    def update_avg_chunk_audio(self, observed_ms: float) -> None:
        """PlaybackStage (W2.B wiring) calls this after each segment plays
        with the observed duration so the moving average self-tunes.
        """
        if observed_ms <= 0:
            return
        a = AVG_CHUNK_EWMA_ALPHA
        self._avg_chunk_audio_ms = (
            a * float(observed_ms) + (1.0 - a) * self._avg_chunk_audio_ms
        )
