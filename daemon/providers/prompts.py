"""Shared prompt builders so every provider judges identically."""


def build_judge_hint(tool_name: str, context: str = "") -> str:
    """The BINARY_JUDGMENT context hint (relocated verbatim from
    content_router._binary_llm_judge). Bounds the context phrase defensively."""
    ctx = (context or "").strip()[:160]
    return (
        f"BINARY_JUDGMENT: tool={tool_name}. "
        f"{'Context: ' + ctx + '. ' if ctx else ''}"
        f"Reply with exactly 'SPEAK' or 'SKIP' — nothing else. "
        f"SPEAK if a TTS readout would surface a meaningful finding "
        f"(test counts, error messages, useful numbers, status pivots) "
        f"relevant to the active work above. "
        f"SKIP if it is mechanical noise or repeats output already spoken."
    )
