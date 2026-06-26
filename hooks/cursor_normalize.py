#!/usr/bin/env python3
"""Normalize Cursor agent hook JSON into Claude Code TTS daemon shape.

Cursor and Claude Code share similar hook concepts but differ in field names:
  conversation_id -> session_id
  tool_output (JSON string) -> tool_response (object)
  Shell -> Bash (ContentRouter extractor registration)

Used by cursor-* hook wrappers before delegating to the existing Claude hooks.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

# Cursor Shell output often includes daemon/log lines with spoken-noise timestamps.
_LOG_LINE_PREFIX_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?"
    r"\s*(?:\[\s*[^\]]+\])?\s*"
)
_HOOK_TIME_PREFIX_RE = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*")
_ISO_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?\b"
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NATURAL_DATE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)
_TODAY_DATE_LINE_RE = re.compile(
    r"(?im)^\s*Today(?:'s)?\s+date\s*:\s*.+$",
)

# Map Cursor tool names to the names ContentRouter / pre-tool-use expect.
TOOL_NAME_MAP: dict[str, str] = {
    "Shell": "Bash",
    "AskQuestion": "AskUserQuestion",
}


def _map_tool_name(name: str) -> str:
    return TOOL_NAME_MAP.get(name, name)


def _collapse_ws(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _sanitize_line(line: str) -> str:
    line = _LOG_LINE_PREFIX_RE.sub("", line)
    line = _HOOK_TIME_PREFIX_RE.sub("", line)
    line = _ISO_DATETIME_RE.sub(" ", line)
    line = _ISO_DATE_RE.sub(" ", line)
    line = _NATURAL_DATE_RE.sub(" ", line)
    return _collapse_ws(line)


def sanitize_spoken_text(text: str) -> str:
    """Strip dates/times Cursor often injects into tool output or responses."""
    if not text or not isinstance(text, str):
        return ""
    cleaned = _TODAY_DATE_LINE_RE.sub("", text)
    lines = []
    for raw in cleaned.splitlines():
        line = _sanitize_line(raw)
        if line:
            lines.append(line)
    if not lines:
        return ""
    joined = "\n".join(lines)
    joined = _ISO_DATETIME_RE.sub(" ", joined)
    joined = _ISO_DATE_RE.sub(" ", joined)
    joined = _NATURAL_DATE_RE.sub(" ", joined)
    return _collapse_ws(joined)


def _empty_tool_response() -> dict[str, Any]:
    return {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
    }


def _parse_tool_output(raw: Any) -> dict[str, Any]:
    """Convert Cursor tool_output into Claude Code tool_response."""
    if raw is None:
        return _empty_tool_response()

    parsed: Any = raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return _empty_tool_response()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {
                **_empty_tool_response(),
                "stdout": raw,
            }

    if isinstance(parsed, dict):
        stdout = parsed.get("stdout")
        if stdout is None and "output" in parsed:
            stdout = parsed.get("output")
        if stdout is None and isinstance(parsed.get("content"), str):
            stdout = parsed.get("content")
        if stdout is None and isinstance(parsed.get("text"), str):
            stdout = parsed.get("text")
        if stdout is None and "file" in parsed and isinstance(parsed["file"], dict):
            file_obj = parsed["file"]
            stdout = file_obj.get("content") or file_obj.get("text") or ""
        if stdout is None:
            stdout = json.dumps(parsed, ensure_ascii=False) if parsed else ""

        if not isinstance(stdout, str):
            stdout = json.dumps(stdout, ensure_ascii=False)
        stdout = sanitize_spoken_text(stdout)

        stderr = parsed.get("stderr") or ""
        if not isinstance(stderr, str):
            stderr = str(stderr)
        stderr = sanitize_spoken_text(stderr)

        exit_code = parsed.get("exitCode", parsed.get("exit_code", parsed.get("code")))
        interrupted = bool(parsed.get("interrupted", False))
        if exit_code not in (None, 0) and not stderr:
            stderr = f"exit code {exit_code}"

        return {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": interrupted,
            "isImage": bool(parsed.get("isImage", parsed.get("is_image", False))),
            "noOutputExpected": bool(
                parsed.get("noOutputExpected", parsed.get("no_output_expected", False))
            ),
        }

    if isinstance(parsed, list):
        return {
            **_empty_tool_response(),
            "stdout": json.dumps(parsed, ensure_ascii=False),
        }

    return {
        **_empty_tool_response(),
        "stdout": str(parsed),
    }


def _adapt_ask_question_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Cursor AskQuestion uses prompt; Claude pre-tool hook expects question."""
    if not isinstance(tool_input, dict):
        return {}
    if "questions" in tool_input:
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict) and "question" not in first and "prompt" in first:
                adapted = dict(tool_input)
                adapted_questions = []
                for q in questions:
                    if not isinstance(q, dict):
                        adapted_questions.append(q)
                        continue
                    item = dict(q)
                    if "question" not in item and "prompt" in item:
                        item["question"] = item["prompt"]
                    adapted_questions.append(item)
                adapted["questions"] = adapted_questions
                return adapted
        return tool_input
    return tool_input


def normalize_tool_hook(payload: dict[str, Any], phase: str) -> dict[str, Any]:
    out = dict(payload)
    out["session_id"] = (
        payload.get("session_id")
        or payload.get("conversation_id")
        or "default"
    )
    out["tool_name"] = _map_tool_name(str(payload.get("tool_name") or ""))

    if not out.get("hook_event_name"):
        out["hook_event_name"] = "PreToolUse" if phase == "pre" else "PostToolUse"

    cwd = payload.get("cwd")
    if not cwd:
        roots = payload.get("workspace_roots") or []
        if roots and isinstance(roots[0], str):
            cwd = roots[0]
    if cwd:
        out["cwd"] = cwd

    tool_input = payload.get("tool_input")
    if out["tool_name"] == "AskUserQuestion" and isinstance(tool_input, dict):
        out["tool_input"] = _adapt_ask_question_input(tool_input)
    elif isinstance(tool_input, dict):
        out["tool_input"] = tool_input
    else:
        out["tool_input"] = tool_input if tool_input is not None else {}

    if phase == "post":
        if "tool_response" not in payload:
            out["tool_response"] = _parse_tool_output(payload.get("tool_output"))
        duration = payload.get("duration_ms", payload.get("duration"))
        if duration is not None and "duration_ms" not in out:
            out["duration_ms"] = duration

    return out


def normalize_agent_response(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text") or payload.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    return {
        "session_id": payload.get("conversation_id")
        or payload.get("session_id")
        or "default",
        "content": sanitize_spoken_text(text),
        "transcript_path": payload.get("transcript_path") or "",
        "stop_hook_active": False,
    }


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "post"
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(1)
    payload = json.loads(raw)
    if phase in ("post", "pre"):
        print(json.dumps(normalize_tool_hook(payload, phase), ensure_ascii=False))
    elif phase == "agent_response":
        print(json.dumps(normalize_agent_response(payload), ensure_ascii=False))
    else:
        sys.stderr.write(f"unknown phase: {phase}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
