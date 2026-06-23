"""
Tests for _recv_line accumulator (DAEMON-01).

Process Gate: Tests written BEFORE implementation.
RED phase: these tests fail with ImportError on _recv_line (before Task 3).
GREEN phase: all four tests pass after _recv_line is implemented.
"""
import json
import socket
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Late import so collection does not crash before implementation exists.
# Each test imports _recv_line at call time.


FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "large_tool_event_payload.json"


def _get_recv_line():
    from daemon.tts_daemon import _recv_line  # noqa: E402
    return _recv_line


def _make_socketpair():
    """Return (server_sock, client_sock) via socketpair."""
    server, client = socket.socketpair()
    return server, client


# ---------------------------------------------------------------------------
# Test 1: _recv_line reads a 10 KB payload terminated by newline
# ---------------------------------------------------------------------------

def test_recv_line_reads_10kb_payload_terminated_by_newline():
    _recv_line = _get_recv_line()

    payload_dict = {"command": "tool_event", "filler": "x" * 10_000}
    payload_bytes = json.dumps(payload_dict).encode() + b"\n"

    server, client = _make_socketpair()

    def sender():
        try:
            client.sendall(payload_bytes)
            client.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    t = threading.Thread(target=sender, daemon=True)
    t.start()

    result = _recv_line(server, max_size=1_048_576)
    t.join(timeout=2.0)
    server.close()

    assert result.endswith(b"\n"), "result must end with newline"
    decoded = result.decode("utf-8")
    parsed = json.loads(decoded.rstrip("\n"))
    assert len(parsed["filler"]) == 10_000


# ---------------------------------------------------------------------------
# Test 2: _recv_line raises ValueError when payload exceeds size cap
# ---------------------------------------------------------------------------

def test_recv_line_raises_on_oversize_payload():
    _recv_line = _get_recv_line()

    server, client = _make_socketpair()
    two_mb = b"X" * (2 * 1_048_576)

    def sender():
        try:
            client.sendall(two_mb)
            client.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    t = threading.Thread(target=sender, daemon=True)
    t.start()

    raised = False
    try:
        _recv_line(server, max_size=1_048_576)
    except ValueError:
        raised = True
    finally:
        t.join(timeout=5.0)
        server.close()

    assert raised, "_recv_line must raise ValueError on oversize payload"


# ---------------------------------------------------------------------------
# Test 3: _recv_line returns partial bytes on EOF before newline
# ---------------------------------------------------------------------------

def test_recv_line_returns_what_it_has_on_eof_before_newline():
    _recv_line = _get_recv_line()

    payload = b'{"command":"tool_event","x":1}'  # no trailing newline

    server, client = _make_socketpair()

    def sender():
        try:
            client.sendall(payload)
            client.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    t = threading.Thread(target=sender, daemon=True)
    t.start()

    result = _recv_line(server, max_size=1_048_576)
    t.join(timeout=2.0)
    server.close()

    assert result == payload, (
        f"Expected partial payload returned on EOF; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: _recv_line handles the deterministic-truncation fixture
# Negative half: recv(4096) on the fixture raises Unterminated string (proves bug)
# Positive half: _recv_line returns full fixture (proves fix)
# ---------------------------------------------------------------------------

def test_recv_line_handles_deterministic_truncation_fixture():
    _recv_line = _get_recv_line()

    assert FIXTURE_PATH.exists(), f"Fixture missing: {FIXTURE_PATH}"
    fixture_bytes = FIXTURE_PATH.read_bytes()
    fixture_size = len(fixture_bytes)
    assert fixture_size > 4096, f"Fixture must be >4096 bytes; got {fixture_size}"

    # --- Negative half: simulate OLD recv(4096) code path ---
    # This proves the fixture has teeth: truncation deterministically produces
    # Unterminated string. A future regression reintroducing recv(4096) will
    # fail this assertion. Addresses VERIFICATION.md B-1.
    server_neg, client_neg = _make_socketpair()

    def sender_neg():
        try:
            client_neg.sendall(fixture_bytes + b"\n")
            client_neg.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    t_neg = threading.Thread(target=sender_neg, daemon=True)
    t_neg.start()

    truncated = server_neg.recv(4096)
    t_neg.join(timeout=2.0)
    server_neg.close()

    json_error_raised = False
    try:
        json.loads(truncated.decode("utf-8"))
    except json.JSONDecodeError as e:
        assert "Unterminated string" in str(e), (
            f"Expected 'Unterminated string' error; got: {e}"
        )
        json_error_raised = True

    assert json_error_raised, (
        "Negative half: recv(4096) on fixture MUST raise JSONDecodeError with "
        "'Unterminated string' — fixture does not prove the bug"
    )

    # --- Positive half: _recv_line reads the full fixture ---
    server_pos, client_pos = _make_socketpair()

    def sender_pos():
        try:
            client_pos.sendall(fixture_bytes + b"\n")
            client_pos.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    t_pos = threading.Thread(target=sender_pos, daemon=True)
    t_pos.start()

    result = _recv_line(server_pos, max_size=1_048_576)
    t_pos.join(timeout=2.0)
    server_pos.close()

    # Full payload received (plus trailing newline)
    assert len(result) == fixture_size + 1, (
        f"Expected {fixture_size + 1} bytes; got {len(result)}"
    )
    assert result.endswith(b"\n")

    # Round-trips to valid JSON
    parsed = json.loads(result.rstrip(b"\n").decode("utf-8"))
    assert parsed["command"] == "tool_event"
    assert parsed["session_id"] == "deterministic-trunc-fixture"
    assert len(parsed["tool_response"]["content"]) == 5000
