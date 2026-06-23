"""Unit tests for the config-gated Voicebox synthesis backend.

Mocks all HTTP so it runs without a live Voicebox app. Covers the client's
POST shape + fail-safe behavior, and that GenerateStage short-circuits (yields
no local audio) when engine == "voicebox".
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.pipeline.voicebox_client import VoiceboxClient  # noqa: E402


class _Resp(io.BytesIO):
    """A urlopen()-style response usable as a context manager + json.load."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(payload, capture):
    def _inner(req, timeout=None):
        data = getattr(req, "data", None)
        if data:
            capture["body"] = json.loads(data.decode())
            capture["method"] = req.get_method()
            capture["url"] = req.full_url
        return _Resp(json.dumps(payload).encode())
    return _inner


def test_speak_posts_expected_body_and_returns_id():
    cap: dict = {}
    c = VoiceboxClient(profile_id="p1", engine="kokoro", personality=False, cleanup=False)
    with patch("urllib.request.urlopen", _fake_urlopen({"id": "gen-123"}, cap)):
        gid = asyncio.run(c.speak("hello world"))
    assert gid == "gen-123"
    assert cap["method"] == "POST"
    assert cap["url"].endswith("/speak")
    assert cap["body"] == {
        "text": "hello world", "personality": False,
        "profile": "p1", "engine": "kokoro",
    }


def test_speak_empty_text_returns_none_without_http():
    c = VoiceboxClient(cleanup=False)
    # No urlopen patch — if it tried to hit the network this would error.
    assert asyncio.run(c.speak("   ")) is None


def test_speak_is_failsafe_on_network_error():
    c = VoiceboxClient(cleanup=False)

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    with patch("urllib.request.urlopen", _boom):
        assert asyncio.run(c.speak("status: build ok")) is None  # swallowed → silence


def test_personality_flag_propagates():
    cap: dict = {}
    c = VoiceboxClient(personality=True, cleanup=False)
    with patch("urllib.request.urlopen", _fake_urlopen({"id": "g"}, cap)):
        asyncio.run(c.speak("recap"))
    assert cap["body"]["personality"] is True


def test_generate_stage_voicebox_short_circuits_to_speak():
    """engine='voicebox' → generate() POSTs the joined utterance and yields
    NO AudioSegments (PlaybackStage no-ops on empty output)."""
    from daemon.pipeline.generate_stage import GenerateStage

    gs = GenerateStage(engine="voicebox",
                       voicebox_config={"profile_id": "p1", "engine": "kokoro", "cleanup": False})
    # Replace the real client with a mock so no HTTP happens.
    gs._voicebox = AsyncMock()
    gs._voicebox.speak = AsyncMock(return_value="gen-9")

    processed = types.SimpleNamespace(
        session_id="s1", request_id="r1",
        chunks=["Tests passed.", "Build OK."],
    )

    async def _collect():
        out = []
        async for seg in gs.generate(processed):
            out.append(seg)
        return out

    segs = asyncio.run(_collect())
    assert segs == []  # no local synthesis
    gs._voicebox.speak.assert_awaited_once()
    spoken = gs._voicebox.speak.call_args.args[0]
    assert "Tests passed." in spoken and "Build OK." in spoken
