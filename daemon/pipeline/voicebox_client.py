"""Voicebox synthesis backend (config-gated, OFF by default).

When ``voice.engine == "voicebox"``, GenerateStage short-circuits local
synthesis and POSTs the cleaned utterance to a locally-running Voicebox app
(https://voicebox.sh) over its REST API. Voicebox owns synthesis AND playback,
so the pipeline yields no AudioSegments — PlaybackStage no-ops, which is an
established safe contract (empty generate output → no audio).

Design notes:
  * REVERSIBLE: flip ``voice.engine`` back to "kokoro". This module is never
    imported unless the flag is set, so it adds zero overhead when off.
  * TOKEN-FREE: this is the daemon calling Voicebox's *local REST API* directly
    (not the model-facing MCP tool), so routing/synthesis costs no Claude tokens.
  * FAIL-SAFE: every network call is wrapped; any error returns None and logs,
    so a Voicebox outage degrades to silence, never a daemon crash. urllib in a
    thread executor keeps the event loop unblocked without a new dependency.
  * SELF-CLEANING: Voicebox is a studio app that persists every generation. To
    keep its history/disk bounded under narration frequency, each utterance is
    deleted after it finishes playing (best-effort; disable via cleanup=false).
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class VoiceboxClient:
    def __init__(
        self,
        url: str = "http://127.0.0.1:17493",
        profile_id: Optional[str] = None,
        engine: Optional[str] = None,
        personality: bool = False,
        cleanup: bool = True,
        timeout_s: float = 10.0,
    ) -> None:
        self.url = (url or "http://127.0.0.1:17493").rstrip("/")
        self.profile_id = profile_id
        self.engine = engine
        # personality=True applies Voicebox's persona LLM rewrite — LOSSY (it can
        # alter facts), so the default is False for faithful status readouts.
        self.personality = bool(personality)
        self.cleanup = bool(cleanup)
        self.timeout_s = float(timeout_s)

    # ---- sync HTTP primitives (run in an executor) ----------------------
    def _post_speak_sync(self, text: str) -> Optional[str]:
        body: dict = {"text": text, "personality": self.personality}
        if self.profile_id:
            body["profile"] = self.profile_id
        if self.engine:
            body["engine"] = self.engine
        req = urllib.request.Request(
            f"{self.url}/speak",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.load(resp).get("id")

    def _get_sync(self, gid: str) -> dict:
        with urllib.request.urlopen(f"{self.url}/history/{gid}", timeout=self.timeout_s) as resp:
            return json.load(resp)

    def _delete_sync(self, gid: str) -> None:
        req = urllib.request.Request(f"{self.url}/history/{gid}", method="DELETE")
        urllib.request.urlopen(req, timeout=self.timeout_s).close()

    # ---- async API ------------------------------------------------------
    async def speak(self, text: str) -> Optional[str]:
        """POST text to Voicebox /speak (synthesize + play locally). Returns the
        generation id, or None on empty input / any failure (logged, swallowed —
        never raises into the pipeline)."""
        if not text or not text.strip():
            return None
        try:
            loop = asyncio.get_event_loop()
            gid = await loop.run_in_executor(None, self._post_speak_sync, text)
            if gid and self.cleanup:
                # fire-and-forget; cleanup waits for playback then deletes
                asyncio.create_task(self._cleanup(gid))
            return gid
        except Exception as e:  # noqa: BLE001 — fail-safe to silence
            logger.warning("voicebox speak failed: %s", e)
            return None

    async def _cleanup(self, gid: str, max_wait_s: float = 60.0) -> None:
        """Wait until the generation finishes playing, then delete it so
        Voicebox's history/disk stays bounded. Best-effort; errors swallowed."""
        loop = asyncio.get_event_loop()
        waited = 0.0
        try:
            duration = 0.0
            while waited < max_wait_s:
                rec = await loop.run_in_executor(None, self._get_sync, gid)
                status = rec.get("status")
                duration = float(rec.get("duration") or 0.0)
                if status in ("completed", "failed", None):
                    break
                await asyncio.sleep(1.5)
                waited += 1.5
            # let playback finish (audio plays as/after it renders) before delete
            await asyncio.sleep(max(2.0, duration + 1.0))
            await loop.run_in_executor(None, self._delete_sync, gid)
            logger.debug("voicebox cleanup deleted generation %s", gid)
        except Exception as e:  # noqa: BLE001
            logger.debug("voicebox cleanup failed for %s: %s", gid, e)
