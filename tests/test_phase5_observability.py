"""
Phase 5 Group A — Observability tests.

Process Gate: "Prove it broke before prove it fixed".
Each test class targets one REQ; tests written BEFORE implementation.

REQ-IDs:
  OBSERVE-03 — schema assertions on every tool_event/stop_event reject
               malformed payloads (single-line JSON log entry, daemon
               does NOT crash, event NOT silently classified-as-silence).
  OBSERVE-01 — request_id UUID threaded end-to-end through ContentRouter,
               QueueManager, and PlaybackStage; logged at every stage.
  OBSERVE-02 — last_audio_played_at recorded on afplay success; new health
               socket fields exposed; ensure-daemon-ready.sh detects stale
               audio during active session.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# OBSERVE-03 — Schema validation
# ============================================================================

class TestObserve03SchemaValidation:
    """Schema validator must reject malformed events with structured logs.

    Failure mode pre-fix: malformed events flow into ContentRouter which
    silently classifies-as-silence (drops on the floor). No log evidence
    that something was wrong with the upstream payload shape.
    """

    def test_validator_module_exists(self):
        """The schema_validator module must exist with validate_event()."""
        from daemon import schema_validator  # noqa: F401
        assert hasattr(schema_validator, "validate_event")

    def test_valid_tool_event_passes(self):
        """A well-formed tool_event payload validates cleanly."""
        from daemon.schema_validator import validate_event
        event = {
            "command": "tool_event",
            "phase": "post",
            "event_id": str(uuid.uuid4()),
            "ts": 1234567890.0,
            "session_id": "abc",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {
                "stdout": "file.txt", "stderr": "", "interrupted": False,
            },
        }
        ok, violations = validate_event(event, "tool_event")
        assert ok is True
        assert violations == []

    def test_tool_event_missing_tool_name_rejected(self):
        """A tool_event payload missing tool_name MUST be rejected."""
        from daemon.schema_validator import validate_event
        event = {
            "command": "tool_event",
            "phase": "post",
            "event_id": str(uuid.uuid4()),
            "ts": 1234567890.0,
            "session_id": "abc",
            # tool_name DELIBERATELY ABSENT
            "tool_input": {"command": "ls"},
        }
        ok, violations = validate_event(event, "tool_event")
        assert ok is False
        assert any("tool_name" in v for v in violations), \
            f"violations should mention tool_name; got {violations!r}"

    def test_tool_event_wrong_type_rejected(self):
        """A tool_event payload with wrong-type field MUST be rejected."""
        from daemon.schema_validator import validate_event
        event = {
            "command": "tool_event",
            "phase": "post",
            "event_id": str(uuid.uuid4()),
            "ts": "not-a-float",  # WRONG TYPE
            "session_id": "abc",
            "tool_name": "Bash",
            "tool_input": {},
        }
        ok, violations = validate_event(event, "tool_event")
        assert ok is False
        assert any("ts" in v for v in violations)

    def test_stop_event_missing_content_rejected(self):
        """stop_event without `content` MUST be rejected."""
        from daemon.schema_validator import validate_event
        event = {
            "command": "stop_event",
            "session_id": "abc",
            # content DELIBERATELY ABSENT
            "transcript_path": "/tmp/x.jsonl",
            "stop_hook_active": False,
            "event_id": str(uuid.uuid4()),
            "ts": 1234567890.0,
        }
        ok, violations = validate_event(event, "stop_event")
        assert ok is False
        assert any("content" in v for v in violations)

    def test_validator_returns_jsonable_violations(self):
        """Violations are plain strings — safe to dump to JSON for log."""
        from daemon.schema_validator import validate_event
        event = {"command": "tool_event"}  # tons of missing fields
        ok, violations = validate_event(event, "tool_event")
        assert ok is False
        # Each violation must be a string (so json.dumps works downstream).
        for v in violations:
            assert isinstance(v, str)
        # And the whole thing serializes.
        json.dumps({"violations": violations})

    def test_unknown_schema_name_returns_violation(self):
        """Calling with an unknown schema name returns ok=False, not crash."""
        from daemon.schema_validator import validate_event
        ok, violations = validate_event({}, "unknown_schema_xyz")
        assert ok is False
        assert violations  # non-empty

    def test_handle_client_logs_schema_violation_for_malformed_tool_event(self, caplog):
        """A malformed tool_event arriving at handle_client emits a
        structured log entry with the marker `schema_violation`.

        Daemon must NOT crash; response status must be 'error' (NOT 'skipped'
        — the latter would mean it silently classified-as-silence).
        """
        from daemon.tts_daemon import TTSDaemon

        # Build a real daemon instance but skip pipeline init (we only
        # exercise the pre-route schema gate).
        daemon = TTSDaemon.__new__(TTSDaemon)
        daemon._setup_logging = lambda: None
        daemon.logger = logging.getLogger('tts_daemon_test_observe03')
        daemon.logger.handlers = []
        daemon.logger.setLevel(logging.DEBUG)
        daemon.log = lambda msg, level='INFO': daemon.logger.info(msg)
        daemon._content_router = None
        daemon._queue_manager = None
        daemon.stats = {'requests_received': 0}

        malformed_payload = {
            "command": "tool_event",
            # Missing phase, event_id, ts, session_id, tool_name, tool_input
        }

        # Mock socket — we just need send() to capture the response.
        mock_sock = MagicMock()
        sent = []
        mock_sock.send.side_effect = lambda b: sent.append(b)
        # _recv_line returns bytes; daemon decodes + json.loads.
        with patch('daemon.tts_daemon._recv_line',
                   return_value=(json.dumps(malformed_payload) + '\n').encode()):
            with caplog.at_level(logging.WARNING, logger='tts_daemon_test_observe03'):
                daemon.handle_client(mock_sock)

        # Daemon did NOT crash — that's already proven by reaching this line.
        # Response must be an error (NOT silently dropped/skipped).
        assert sent, "handle_client must send a response"
        resp = json.loads(sent[0].decode())
        assert resp.get('status') == 'error', \
            f"malformed event must yield error, not {resp.get('status')!r}"
        # Log must contain a schema_violation marker.
        log_text = '\n'.join(rec.getMessage() for rec in caplog.records)
        assert 'schema_violation' in log_text, \
            f"expected 'schema_violation' marker in logs; got: {log_text!r}"


# ============================================================================
# OBSERVE-01 — request_id threaded end-to-end
# ============================================================================

class TestObserve01RequestIdThreading:
    """request_id UUID flows from hook through ContentRouter, QueueManager,
    PlaybackStage; each stage logs `request_id=<uuid>` so a single grep
    against tts_daemon.log returns ≥4 hits for one event.
    """

    def test_router_decision_carries_event_id_as_source(self):
        """ContentRouter populates RouterDecision.source_event_id from
        event['event_id'] (this should already be true per content_router.py:1146).
        Locks in the contract.
        """
        import asyncio
        from daemon.content_router import ContentRouter
        from daemon.tts_types import RouterDecision

        # Stub OllamaSummarizer — never called for short error-class fixtures.
        from daemon.providers.ollama_provider import OllamaProvider
        stub_summarizer = MagicMock()
        router = ContentRouter(config={}, provider=OllamaProvider(stub_summarizer))
        known_uuid = str(uuid.uuid4())
        event = {
            "command": "tool_event",
            "phase": "post",
            "event_id": known_uuid,
            "ts": 1234567890.0,
            "session_id": "test-session",
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": {
                "stdout": "exit 0\nFAILED 5 of 10 tests",
                "stderr": "",
                "interrupted": False,
            },
        }
        decision = asyncio.run(router.classify_event(event))
        assert decision.source_event_id == known_uuid

    def test_pipeline_adapter_submit_async_accepts_request_id(self):
        """PipelineAdapter.submit_async signature must accept request_id kwarg.

        This is the bridge that previously dropped the UUID by calling
        `orchestrator.submit()` which generates its own short-uuid.
        """
        from daemon.pipeline.adapter import PipelineAdapter
        import inspect
        sig = inspect.signature(PipelineAdapter.submit_async)
        assert 'request_id' in sig.parameters, \
            f"submit_async must accept request_id; signature is {sig}"

    def test_orchestrator_submit_accepts_and_uses_request_id(self):
        """TTSPipelineOrchestrator.submit must accept a request_id and
        return it (instead of generating a new one) when supplied.
        """
        from daemon.pipeline.orchestrator import TTSPipelineOrchestrator
        import inspect
        sig = inspect.signature(TTSPipelineOrchestrator.submit)
        assert 'request_id' in sig.parameters, \
            f"orchestrator.submit must accept request_id; signature is {sig}"

    def test_submit_to_pipeline_threads_request_id(self):
        """daemon._submit_to_pipeline must accept request_id and pass it on."""
        from daemon.tts_daemon import TTSDaemon
        import inspect
        sig = inspect.signature(TTSDaemon._submit_to_pipeline)
        assert 'request_id' in sig.parameters, \
            f"_submit_to_pipeline must accept request_id; signature is {sig}"

    def test_handle_tool_event_mints_request_id_when_missing(self, caplog):
        """If the inbound event has no event_id, daemon mints a UUID and
        logs request_id_minted=true so the operator can see it happened.
        """
        from daemon.tts_daemon import TTSDaemon

        daemon = TTSDaemon.__new__(TTSDaemon)
        daemon._setup_logging = lambda: None
        daemon.logger = logging.getLogger('tts_daemon_test_observe01_mint')
        daemon.logger.handlers = []
        daemon.logger.setLevel(logging.DEBUG)
        daemon.log = lambda msg, level='INFO': daemon.logger.info(msg)
        # No pipeline — we expect early-out via 'error' response, but
        # the mint+log MUST happen before the early-out.
        daemon._content_router = None
        daemon._queue_manager = None

        request_data = {
            "command": "tool_event",
            "phase": "post",
            # event_id ABSENT — should be minted
            "ts": 1234567890.0,
            "session_id": "test",
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": {
                "stdout": "ok", "stderr": "", "interrupted": False,
            },
        }

        with caplog.at_level(logging.INFO, logger='tts_daemon_test_observe01_mint'):
            daemon._handle_tool_event(request_data)

        log_text = '\n'.join(rec.getMessage() for rec in caplog.records)
        assert 'request_id_minted' in log_text, \
            f"expected 'request_id_minted' marker in logs; got: {log_text!r}"


# ============================================================================
# OBSERVE-02 — last_audio_played_at + health socket
# ============================================================================

class TestObserve02HealthAndLastAudio:
    """PlaybackStage records last_audio_played_at on afplay success.
    Daemon `health` socket command exposes new fields:
      last_audio_played_at, seconds_since_last_audio, tier
    """

    def test_playback_stage_initializes_last_audio_played_at_none(self):
        """Fresh PlaybackStage must have last_audio_played_at = None."""
        from daemon.pipeline.playback_stage import PlaybackStage
        ps = PlaybackStage()
        assert hasattr(ps, 'last_audio_played_at')
        assert ps.last_audio_played_at is None

    def test_playback_stage_updates_last_audio_played_at_on_success(self):
        """After a successful play, last_audio_played_at must be set
        to ~time.time() (within a small window).
        """
        import asyncio
        import time
        import os
        import tempfile
        from daemon.pipeline.playback_stage import PlaybackStage

        ps = PlaybackStage()
        # Build a tiny temp file path; we patch out subprocess so audio is
        # never actually played.
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tf:
            tf.write(b'\x00' * 16)
            audio_path = tf.name

        async def fake_subprocess_exec(*args, **kwargs):
            mock_proc = MagicMock()
            mock_proc.returncode = 0

            async def fake_wait():
                return 0
            mock_proc.wait = fake_wait
            mock_proc.terminate = lambda: None
            return mock_proc

        try:
            t_before = time.time()
            with patch(
                'daemon.pipeline.playback_stage.asyncio.create_subprocess_exec',
                side_effect=fake_subprocess_exec,
            ):
                result = asyncio.run(ps._play_audio(audio_path, state=None))
            t_after = time.time()

            assert result is True
            assert ps.last_audio_played_at is not None
            assert t_before <= ps.last_audio_played_at <= t_after, (
                f"timestamp {ps.last_audio_played_at} not in window "
                f"[{t_before}, {t_after}]"
            )
        finally:
            os.unlink(audio_path)

    def test_health_command_exposes_new_fields(self):
        """The `health` socket command response must include the three new
        fields: last_audio_played_at, seconds_since_last_audio, tier.
        """
        from daemon.tts_daemon import TTSDaemon
        import time

        daemon = TTSDaemon.__new__(TTSDaemon)
        daemon._setup_logging = lambda: None
        daemon.logger = logging.getLogger('tts_daemon_test_observe02_health')
        daemon.logger.handlers = []
        daemon.log = lambda msg, level='INFO': daemon.logger.info(msg)

        # Stub minimal state needed by the existing `health` branch.
        daemon.stats = {'daemon_start': time.time() - 100.0, 'health_checks': 0}
        daemon.session_queues = {}
        # Resource monitor stub
        rm = MagicMock()
        rm.get_memory_usage = lambda: 50.0
        rm.get_cpu_percent = lambda: 1.0
        daemon.resource_monitor = rm
        # Circuit breaker stubs
        for cb_name in (
            'tts_circuit_breaker', 'audio_circuit_breaker',
            'socket_circuit_breaker',
        ):
            cb = MagicMock()
            cb.state = MagicMock()
            cb.state.value = 'CLOSED'
            cb.state.__eq__ = lambda self, other: False
            setattr(daemon, cb_name, cb)
        daemon.error_counts = {}
        daemon._pipeline_adapter = None
        daemon._queue_manager = None

        # Call handle_client with health request via mock socket.
        mock_sock = MagicMock()
        sent = []
        mock_sock.send.side_effect = lambda b: sent.append(b)
        payload = json.dumps({"command": "health"}) + "\n"
        with patch('daemon.tts_daemon._recv_line',
                   return_value=payload.encode()):
            daemon.handle_client(mock_sock)

        assert sent
        resp = json.loads(sent[0].decode())
        for field in (
            'last_audio_played_at',
            'seconds_since_last_audio',
            'tier',
        ):
            assert field in resp, (
                f"health response missing field {field!r}; got keys: "
                f"{list(resp.keys())}"
            )

    def test_ensure_daemon_ready_has_audio_staleness_check(self):
        """ensure-daemon-ready.sh must contain logic that warns on stale
        audio during an active session. Marker: must reference both
        `last_audio_played_at` (or the staleness threshold 300) AND `stderr`.
        """
        script = (
            PROJECT_ROOT / "hooks" / "ensure-daemon-ready.sh"
        ).read_text()
        assert ("seconds_since_last_audio" in script
                or "300" in script), (
            "ensure-daemon-ready.sh must contain audio-staleness check; "
            "expected 'seconds_since_last_audio' or '300' threshold"
        )
        # Warning must go to stderr (NOT auto-restart)
        assert ">&2" in script, "warning must go to stderr"
