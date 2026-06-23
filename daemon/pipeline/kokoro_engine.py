"""Daemon-side manager for the persistent Kokoro (mlx-audio) synthesis worker.

Runs under the daemon's own interpreter (Python 3.13). It owns a long-lived
subprocess — ``daemon/kokoro_worker.py`` executed by the *mlx-audio conda
Python* — and exposes a single async ``synthesize()`` call. The worker loads
and warms the Kokoro model exactly once; this manager just streams
newline-delimited JSON requests to it and reads responses.

Why a subprocess and not an in-process import: MLX + mlx_audio are installed
only in the conda env (Apple-silicon wheels, Python 3.12), while the daemon
runs on the 3.13 framework Python. The interpreter boundary is the integration
seam — see ``daemon/kokoro_worker.py`` for the protocol.

Concurrency model: one warm worker, requests serialized behind an asyncio.Lock.
Warm synthesis is ~0.15 s/chunk (~20x real-time), so a single worker sustains
far more throughput than playback consumes, while keeping exactly one model
resident (~150 MB) and avoiding GPU contention from parallel synth.
"""

import asyncio
import json
import os
import shutil
import sys
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Resolve worker script path relative to this file (daemon/pipeline/ -> daemon/).
_WORKER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "kokoro_worker.py")

# Interpreter that has mlx-audio installed. Not discoverable in general (it lives
# in an Apple-silicon conda/venv), so it is configured at setup: env override wins,
# then a python3 on PATH, then the daemon's own interpreter as a last resort.
DEFAULT_MLX_PYTHON = os.environ.get("MLX_PYTHON") or shutil.which("python3") or sys.executable
DEFAULT_MODEL = "mlx-community/Kokoro-82M-bf16"


class KokoroEngine:
    """Owns the persistent Kokoro worker subprocess and serializes synthesis."""

    def __init__(
        self,
        mlx_python: str = DEFAULT_MLX_PYTHON,
        model: str = DEFAULT_MODEL,
        default_voice: str = "af_heart",
        lang_code: str = "a",
        ready_timeout_s: float = 60.0,
        synth_timeout_s: float = 20.0,
    ):
        self.mlx_python = mlx_python
        self.model = model
        self.default_voice = default_voice
        self.lang_code = lang_code
        self.ready_timeout_s = ready_timeout_s
        self.synth_timeout_s = synth_timeout_s

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()        # serialize request/response pairs
        self._spawn_lock = asyncio.Lock()  # serialize (re)spawns
        self._req_counter = 0
        self.sample_rate: Optional[int] = None
        self._stats = {"synth_ok": 0, "synth_err": 0, "respawns": 0,
                       "total_synth_ms": 0.0}

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> bool:
        """Spawn and warm the worker. Returns True once it reports ready."""
        return await self._ensure_alive()

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _ensure_alive(self) -> bool:
        if self._is_alive():
            return True
        async with self._spawn_lock:
            if self._is_alive():  # double-checked after acquiring the lock
                return True
            return await self._spawn()

    async def _spawn(self) -> bool:
        if not os.path.exists(self.mlx_python):
            logger.error("Kokoro mlx python not found: %s", self.mlx_python)
            return False
        if not os.path.exists(_WORKER_PATH):
            logger.error("Kokoro worker script not found: %s", _WORKER_PATH)
            return False

        env = dict(os.environ)
        env["KOKORO_MODEL"] = self.model
        env["KOKORO_VOICE"] = self.default_voice
        env["KOKORO_LANG"] = self.lang_code

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.mlx_python, _WORKER_PATH,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,  # worker logs to stderr; drop it
                env=env,
            )
        except Exception as e:
            logger.error("Failed to spawn Kokoro worker: %s", e)
            self._proc = None
            return False

        # Wait for the one-time ready handshake, skipping any stray lines.
        try:
            t0 = time.time()
            while True:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self.ready_timeout_s
                )
                if not line:  # EOF before ready → worker died during load
                    raise RuntimeError("worker exited before ready")
                try:
                    msg = json.loads(line.decode().strip())
                except Exception:
                    continue
                if "ready" in msg:
                    if not msg.get("ready"):
                        raise RuntimeError(msg.get("error", "worker not ready"))
                    self.sample_rate = msg.get("sample_rate")
                    logger.info("Kokoro worker ready in %.1fs (model=%s sr=%s)",
                                time.time() - t0, self.model, self.sample_rate)
                    return True
        except Exception as e:
            logger.error("Kokoro worker failed to become ready: %s", e)
            await self._kill()
            return False

    async def _kill(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.returncode is None:
                    self._proc.kill()
                    await self._proc.wait()
            except Exception:
                pass
            self._proc = None

    async def shutdown(self) -> None:
        """Close stdin (worker exits cleanly) then ensure the process is gone."""
        async with self._spawn_lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin and not self._proc.stdin.is_closing():
                        self._proc.stdin.close()
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except Exception:
                    await self._kill()
                self._proc = None

    # ---- synthesis -------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        out_path: str,
        voice: Optional[str] = None,
        speed: float = 1.0,
    ) -> bool:
        """Synthesize ``text`` to ``out_path`` (.wav). Returns True on success.

        On worker death the call respawns once and retries, so a crashed worker
        self-heals on the next utterance rather than silently going mute.
        """
        for attempt in (1, 2):
            if not await self._ensure_alive():
                return False
            try:
                return await self._do_synthesize(text, out_path, voice, speed)
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as e:
                logger.warning("Kokoro synth attempt %d failed (%s); respawning",
                               attempt, e)
                self._stats["respawns"] += 1
                await self._kill()
                continue
            except Exception as e:
                logger.error("Kokoro synth error: %s", e)
                self._stats["synth_err"] += 1
                return False
        return False

    async def _do_synthesize(self, text, out_path, voice, speed) -> bool:
        async with self._lock:  # one in-flight request at a time
            assert self._proc is not None and self._proc.stdin is not None
            self._req_counter += 1
            req_id = f"r{self._req_counter}"
            req = {
                "id": req_id,
                "text": text,
                "out_path": out_path,
                "voice": voice or self.default_voice,
                "speed": speed,
                "lang_code": self.lang_code,
            }
            self._proc.stdin.write((json.dumps(req) + "\n").encode())
            await self._proc.stdin.drain()

            # Read until we get the response matching our id (skip stray lines).
            while True:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self.synth_timeout_s
                )
                if not line:
                    raise RuntimeError("worker closed stdout mid-request")
                try:
                    resp = json.loads(line.decode().strip())
                except Exception:
                    continue
                if resp.get("id") != req_id:
                    continue
                if resp.get("ok"):
                    self._stats["synth_ok"] += 1
                    self._stats["total_synth_ms"] += resp.get("synth_ms", 0.0)
                    return True
                self._stats["synth_err"] += 1
                logger.error("Kokoro worker reported failure: %s",
                             resp.get("error"))
                return False

    def get_stats(self) -> dict:
        ok = self._stats["synth_ok"]
        avg = (self._stats["total_synth_ms"] / ok) if ok else 0.0
        return {**self._stats, "avg_synth_ms": round(avg, 1),
                "alive": self._is_alive()}
