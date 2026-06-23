"""Regression tests for Phase 5 OBSERVE-01/03 hotfixes.

The Group A executor wired schema validation strict-by-default for all
tools but only Bash actually has stdout/stderr/interrupted in tool_response.
The Group A executor wired request_id logging in queue_manager.intercept()
but ERRORs bypass intercept() via submit_priority(), which had no log line.

Both behaviors must be regression-locked.
"""
import sys
sys.path.insert(0, "/home/user/project")

from daemon.schema_validator import validate_event


def test_bash_event_with_full_response_validates():
    payload = {
        "command": "tool_event", "phase": "post",
        "event_id": "u1", "ts": 1.0,
        "session_id": "s", "tool_name": "Bash",
        "tool_input": {"command": "ls"}, "tool_use_id": "x",
        "tool_response": {
            "stdout": "ok", "stderr": "", "interrupted": False,
        },
    }
    ok, viols = validate_event(payload, "tool_event")
    assert ok, f"expected pass, got: {viols}"


def test_bash_event_missing_stdout_rejected():
    payload = {
        "command": "tool_event", "phase": "post",
        "event_id": "u1", "ts": 1.0,
        "session_id": "s", "tool_name": "Bash",
        "tool_input": {"command": "ls"}, "tool_use_id": "x",
        "tool_response": {"stderr": "", "interrupted": False},
    }
    ok, viols = validate_event(payload, "tool_event")
    assert not ok, "Bash event missing stdout should reject"
    assert any("stdout" in v for v in viols)


def test_read_event_without_bash_fields_validates():
    """Regression: Read tool_response has no stdout/stderr/interrupted —
    schema must NOT require them. Pre-fix, this rejected with 3 violations
    and silently dropped every Read event from the live pipeline."""
    payload = {
        "command": "tool_event", "phase": "post",
        "event_id": "u1", "ts": 1.0,
        "session_id": "s", "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo"}, "tool_use_id": "x",
        "tool_response": {"file_content": "<file body>"},
    }
    ok, viols = validate_event(payload, "tool_event")
    assert ok, f"Read event should pass schema; got: {viols}"


def test_edit_event_with_diff_response_validates():
    """Edit tool returns a diff-shaped response, not stdout/stderr."""
    payload = {
        "command": "tool_event", "phase": "post",
        "event_id": "u1", "ts": 1.0,
        "session_id": "s", "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/foo", "old": "a", "new": "b"},
        "tool_use_id": "x",
        "tool_response": {"old_string": "a", "new_string": "b"},
    }
    ok, viols = validate_event(payload, "tool_event")
    assert ok, f"Edit event should pass schema; got: {viols}"


if __name__ == "__main__":
    test_bash_event_with_full_response_validates()
    test_bash_event_missing_stdout_rejected()
    test_read_event_without_bash_fields_validates()
    test_edit_event_with_diff_response_validates()
    print("PASS: 4/4")
