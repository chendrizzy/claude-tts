"""Shared type definitions for the TTS pipeline overhaul.

Imported by: content_router, ollama_summarizer, pipeline.queue_manager,
pipeline.orchestrator, tts_daemon. Stdlib-only dependencies.

Pre-created in Wave 0.5 from the plan's interface spec so the 6 parallel
Wave 1 agents share canonical type definitions and don't drift.

DO NOT add module imports beyond stdlib here — keep this dependency-free
so it sits at the bottom of the import graph.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable, Any
import time


class Category(str, Enum):
    """Routed-content category. String values are stable for log/JSON serialization."""
    ERROR        = "error"          # exit_code != 0, stderr, panic/exception keywords
    FINAL_ANSWER = "final_answer"   # assistant_event terminating a turn (Stop within ~200ms)
    INSIGHT      = "insight"        # ★ Insight markers, "I see / I found / The root cause"
    STATUS       = "status"         # Substantive tool result (test counts, grep N, build output)


class Tier(str, Enum):
    """QueueManager pressure tier. String values are stable for telemetry."""
    GREEN  = "green"   # lag < 3000ms        — pass-through
    YELLOW = "yellow"  # 3000-8000ms         — coalesce same-category pairs
    RED    = "red"     # 8000-15000ms        — drop low-pri, force-summarize
    BLACK  = "black"   # >15000ms or qd>50   — nuclear flush


# Pressure multipliers indexed by Tier. ContentRouter multiplies its decision
# threshold by this to apply backpressure during high lag.
PRESSURE_MULTIPLIER: dict[Tier, float] = {
    Tier.GREEN:  1.0,
    Tier.YELLOW: 1.5,
    Tier.RED:    2.5,
    Tier.BLACK:  5.0,
}

# Priority constants — keep aligned with the Priority enum in tts_daemon.py
# (1 = lowest, 10 = highest urgency).
PRIORITY_LOW    = 3
PRIORITY_NORMAL = 5
PRIORITY_HIGH   = 7
PRIORITY_ERROR  = 10  # ERROR always pre-empts


@dataclass
class RouterDecision:
    """Output of ContentRouter.classify_event — does NOT yet imply submission.

    QueueManager and TurnBuffer consume this. If `should_speak=False`, downstream
    must drop silently (no fallback to legacy speak path).
    """
    should_speak: bool
    category: Category
    content: str                           # Raw or summarized text ready for TTS
    priority: int                          # 1-10, see PRIORITY_* constants
    source_event_id: str                   # Echo of EventPayload.event_id for traceability
    classified_at: float                   # time.time() when classification finished
    needs_summarization: bool = False      # True if >120 chars and not yet summarized
    context_hint: str = ""                 # e.g., "test result" — passed to summarizer
    raw_excerpt: str = ""                  # First 80 chars of pre-summarization input (for logs)


@dataclass
class RoutedItem:
    """What ContentRouter emits to QueueManager — wraps RouterDecision with submission metadata.

    Returned by ContentRouter.route() (the public submission entry-point).
    QueueManager.intercept consumes a list of these.
    """
    decision: RouterDecision
    session_id: str
    submitted_at: float = field(default_factory=time.time)
    enqueued_at: Optional[float] = None
    is_condensed_from: list[str] = field(default_factory=list)  # source_event_ids


# ===== Socket payload schemas =====
# JSON dicts (not dataclasses) — socket protocol is JSON.
# tts_daemon.py validates incoming requests against required keys here.
# Schemas reflect the ACTUAL Claude Code hook payloads (empirically verified).

# Sent by hooks/post-tool-use.sh (and pre-tool-use.sh with phase="pre").
# Hook script forwards the raw Claude Code payload PLUS wrapping fields:
#   command, phase, event_id, ts. All other fields are passthrough.
TOOL_EVENT_SCHEMA = {
    "command": "tool_event",          # str, required, literal
    "phase": "pre|post",              # str, required (added by hook script)
    "event_id": "<uuid4>",            # str, required (hook script generates)
    "ts": 0.0,                        # float, required (time.time() at hook fire)
    # === Claude Code passthrough fields (post-tool example) ===
    "session_id": "<str>",            # required; from JSON payload (NOT env var)
    "transcript_path": "<str>",       # path to JSONL transcript
    "cwd": "<str>",                   # current working directory
    "permission_mode": "<str>",       # e.g., "bypassPermissions"
    "hook_event_name": "PostToolUse", # echoed
    "tool_name": "<str>",             # required (e.g., "Bash", "Read")
    "tool_input": {},                 # dict, required (raw tool args)
    "tool_use_id": "<str>",           # tool invocation UUID
    "duration_ms": 0,                 # int (post only); useful for long-running heuristic
    # === Tool result is NESTED under tool_response ===
    # NOTE: Claude Code does NOT expose subprocess exit_code. ERROR detection
    # must use stderr non-empty + content patterns on stdout/stderr.
    "tool_response": {                # post only; absent on pre
        "stdout": "<str>",            # primary content
        "stderr": "<str>",            # error stream (may be empty even on errors if 2>&1 used)
        "interrupted": False,         # bool — partial output flag
        "isImage": False,             # bool
        "noOutputExpected": False,    # bool
    },
}

# Sent by hooks/speech_output_hook.sh (now wired to the Stop hook).
# Hook script reads transcript_path from Claude Code's Stop payload, parses
# the JSONL, extracts the latest assistant message text, and posts it.
# (Replaces the earlier ASSISTANT_EVENT_SCHEMA — there is no PostAssistantMessage
# hook event in Claude Code; Stop is the only path to assistant content.)
STOP_EVENT_SCHEMA = {
    "command": "stop_event",          # str, required, literal
    "session_id": "<str>",            # required; from Stop payload JSON
    "content": "<str>",               # required; latest assistant message text
    "transcript_path": "<str>",       # for daemon to re-read if needed
    "stop_hook_active": False,        # bool — True if forced continuation (prevents infinite loops)
    "event_id": "<uuid4>",            # required
    "ts": 0.0,                        # float, required
}

# Daemon response (both endpoints share schema):
EVENT_RESPONSE_SCHEMA = {
    "status": "accepted|skipped|error",      # str, required
    "event_id": "<echoed>",                  # str
    "category": "error|final_answer|insight|status|null",   # null if skipped
    "tier": "green|yellow|red|black|null",   # current QM tier
    "queued": True,                          # bool — actually entered the pipeline?
    "reason": "<str>",                       # diagnostic
}


# ===== Type aliases for clarity (not enforced at runtime) =====
SessionId = str
EventId = str
FlushCallback = Callable[[list[RoutedItem]], Awaitable[None]]
