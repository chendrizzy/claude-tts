"""Phase 2 — Cutover Discipline tests.

Covers:
- LEGACY-01: legacy_speak_enabled kill switch in handle_client
- ROUTER-01: _SPEAKABLE_CATEGORIES module constant + guard in _make_decision
- ROUTER-06: events with no recognised category return should_speak=False

Tests are written to FAIL before the implementation changes are applied
(RED phase).  They pass once the fixes land (GREEN phase).
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.content_router import ContentRouter, _SPEAKABLE_CATEGORIES  # noqa: E402
from daemon.tts_types import Category, RouterDecision  # noqa: E402
from daemon.providers.ollama_provider import OllamaProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class MockOllamaSummarizer:
    async def summarize(self, content: str, category: Any, context_hint: str = "") -> str:
        return f"[summary] {content[:40]}"


def _make_router() -> ContentRouter:
    router = ContentRouter(config={}, provider=OllamaProvider(MockOllamaSummarizer()))
    return router


def _stop_event(content: str = "Hello world.") -> dict:
    return {
        "command": "stop_event",
        "event_id": "ev-test-001",
        "ts": time.time(),
        "session_id": "test-session",
        "tool_name": "stop",
        "phase": "post",
        "content": content,
    }


# ---------------------------------------------------------------------------
# LEGACY-01 — legacy_speak_enabled kill switch
# ---------------------------------------------------------------------------

class TestLegacySpeakKillSwitch:
    """LEGACY-01: when config flag is false, speak command returns rejected."""

    def _run_server_with_config(self, config: dict, message: dict) -> dict:
        """Spin up a real socket server with the given config patch and fire
        one `speak` command at it.  Returns the parsed JSON response."""
        import importlib
        import daemon.tts_daemon as td

        # Build a minimal daemon instance — just enough to exercise handle_client.
        # We patch _load_tts_user_config so no real file I/O is needed.
        with patch.object(td.TTSDaemon, "_load_tts_user_config", return_value=config):
            daemon_inst = td.TTSDaemon.__new__(td.TTSDaemon)
            # Minimal attribute init to make handle_client work
            daemon_inst._tts_user_config = config
            daemon_inst.log = lambda *a, **k: None
            daemon_inst.stop_signal = threading.Event()
            daemon_inst.audio_lock = threading.Lock()
            daemon_inst.audio_condition = threading.Condition(daemon_inst.audio_lock)
            daemon_inst.request_queue = []
            daemon_inst.queue_lock = threading.Lock()
            daemon_inst.add_request = MagicMock(return_value=True)
            daemon_inst._get_queue_position = MagicMock(return_value=0)
            daemon_inst._handle_tool_event = MagicMock(return_value={"status": "ok"})
            daemon_inst._handle_stop_event = MagicMock(return_value={"status": "ok"})

            # Create a socket pair to exercise handle_client without a real server.
            server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                raw = (json.dumps(message) + "\n").encode()
                client_sock.sendall(raw)
                client_sock.shutdown(socket.SHUT_WR)

                # Run handle_client in the main thread (synchronous).
                daemon_inst.handle_client(server_sock)

                # Read the response from client side.
                response_raw = b""
                client_sock.settimeout(2.0)
                try:
                    while True:
                        chunk = client_sock.recv(4096)
                        if not chunk:
                            break
                        response_raw += chunk
                except socket.timeout:
                    pass

                return json.loads(response_raw.decode()) if response_raw else {}
            finally:
                try:
                    client_sock.close()
                except Exception:
                    pass
                try:
                    server_sock.close()
                except Exception:
                    pass

    def test_speak_rejected_when_legacy_disabled(self):
        """When feature_flags.legacy_speak_enabled is false, speak returns rejected."""
        config = {"feature_flags": {"legacy_speak_enabled": False}}
        msg = {"command": "speak", "text": "hello", "session_id": "s1"}
        response = self._run_server_with_config(config, msg)

        assert response.get("status") == "rejected", (
            f"Expected 'rejected' but got: {response}"
        )
        assert "legacy" in response.get("reason", "").lower(), (
            f"Expected reason to mention 'legacy', got: {response.get('reason')}"
        )

    def test_speak_accepted_when_legacy_enabled(self):
        """When feature_flags.legacy_speak_enabled is true (default), speak routes normally."""
        config = {"feature_flags": {"legacy_speak_enabled": True}}
        msg = {"command": "speak", "text": "hello", "session_id": "s1"}
        response = self._run_server_with_config(config, msg)

        assert response.get("status") != "rejected", (
            f"Expected normal routing but got rejected: {response}"
        )

    def test_speak_accepted_when_flag_absent(self):
        """When feature_flags section is missing, speak defaults to enabled."""
        config = {}  # no feature_flags at all
        msg = {"command": "speak", "text": "hello", "session_id": "s1"}
        response = self._run_server_with_config(config, msg)

        assert response.get("status") != "rejected", (
            f"Expected normal routing (flag absent = enabled), got: {response}"
        )


# ---------------------------------------------------------------------------
# ROUTER-01 — _SPEAKABLE_CATEGORIES module constant
# ---------------------------------------------------------------------------

class TestSpeakableCategoriesConstant:
    """ROUTER-01: _SPEAKABLE_CATEGORIES must be a module-level frozenset."""

    def test_constant_is_frozenset(self):
        assert isinstance(_SPEAKABLE_CATEGORIES, frozenset), (
            "_SPEAKABLE_CATEGORIES must be a frozenset at module level"
        )

    def test_constant_contains_four_categories(self):
        expected = {Category.ERROR, Category.FINAL_ANSWER, Category.INSIGHT, Category.STATUS}
        assert _SPEAKABLE_CATEGORIES == expected, (
            f"_SPEAKABLE_CATEGORIES should be exactly {expected}, got {_SPEAKABLE_CATEGORIES}"
        )

    def test_make_decision_forces_silence_for_unknown_category(self):
        """If a Category value outside the allowlist is passed, _make_decision
        must override should_speak=True → False.

        We simulate this by monkey-patching Category with an extra value at
        runtime (Python allows it for str-enum variants).
        """
        router = _make_router()

        # Build a synthetic RouterDecision directly via _make_decision using
        # a valid category but with should_speak=True, then assert it is
        # coerced to False for a category NOT in _SPEAKABLE_CATEGORIES.
        # Since we cannot easily add an enum member at test time, we test the
        # inverse: a real SPEAKABLE category still emits should_speak=True
        # when passed as should_speak=True, confirming the guard only blocks
        # non-members.
        event = {"event_id": "ev-r01", "session_id": "s", "tool_name": "stop", "ts": 0}

        # Category.STATUS is in the allowlist — should stay True
        decision_in = router._make_decision(
            event=event,
            should_speak=True,
            category=Category.STATUS,
            content="Test counts: 3 passed.",
            priority=5,
        )
        assert decision_in.should_speak is True, "Allowlisted category should remain should_speak=True"

        # Now test a non-allowlisted category: create a fake Category via
        # dynamic enum extension trick.
        import enum
        # Create a temporary Category member not in _SPEAKABLE_CATEGORIES
        # by directly constructing a RouterDecision bypass path.
        # Instead, verify _make_decision's guard logic by checking behavior
        # when we call it with should_speak=True and a non-allowlisted value.
        # We'll do this by temporarily extending the enum.
        try:
            unknown_cat = Category("unknown_test_only")  # will raise if not in enum
        except ValueError:
            # Good — 'unknown_test_only' is not a valid Category member.
            # Create a mock that acts like a Category but is not in the frozenset.
            unknown_cat = MagicMock(spec=Category)
            unknown_cat.value = "unknown_test_only"
            unknown_cat.__class__ = Category  # so isinstance checks pass

        decision_out = router._make_decision(
            event=event,
            should_speak=True,
            category=unknown_cat,
            content="Some content.",
            priority=5,
        )
        assert decision_out.should_speak is False, (
            f"Non-allowlisted category must force should_speak=False, got {decision_out.should_speak}"
        )


# ---------------------------------------------------------------------------
# ROUTER-06 — default silence for unrecognised events
# ---------------------------------------------------------------------------

class TestDefaultSilencePolicy:
    """ROUTER-06: synthetic events with no recognisable category → should_speak=False."""

    def test_tool_event_with_empty_tool_response_is_silent(self):
        """A tool_event with a completely empty response should be silent."""
        router = _make_router()
        event = {
            "command": "tool_event",
            "event_id": "ev-r06-001",
            "ts": time.time(),
            "session_id": "test-session",
            "tool_name": "SomeFutureUnknownTool",
            "phase": "post",
            "tool_response": "",
        }
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            if hasattr(router, "classify_event"):
                decision = loop.run_until_complete(router.classify_event(event))
            else:
                decision = router._silence(event, "test")
        finally:
            loop.close()

        assert decision.should_speak is False, (
            f"Empty/unknown tool event must be silent, got should_speak={decision.should_speak}"
        )

    def test_stop_event_with_noise_content_is_silent(self):
        """A stop_event whose content is pure boilerplate noise should not speak."""
        router = _make_router()
        event = _stop_event(content="Here is the output you requested:")
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            decision = loop.run_until_complete(router.classify_event(event))
        finally:
            loop.close()

        assert decision.should_speak is False, (
            f"Noise-only stop_event must be silent, got should_speak={decision.should_speak}"
        )

    def test_unrecognised_command_returns_silence(self):
        """An event with an unknown command type must return should_speak=False."""
        router = _make_router()
        event = {
            "command": "mystery_event",
            "event_id": "ev-r06-003",
            "ts": time.time(),
            "session_id": "test-session",
        }
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            decision = loop.run_until_complete(router.classify_event(event))
        finally:
            loop.close()

        assert decision.should_speak is False
