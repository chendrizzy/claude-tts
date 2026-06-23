#!/usr/bin/env python3
"""Persistent Kokoro TTS synthesis worker (mlx-audio backend).

This script runs under the *mlx-audio conda interpreter* — NOT the daemon's
Python 3.13. MLX is Apple-silicon-specific and the mlx_audio package lives only
in that env, so the daemon cannot import it in-process. Instead the daemon
(see ``daemon/pipeline/kokoro_engine.py``) spawns this script once, keeps it
alive, and streams synthesis requests over stdin/stdout as newline-delimited
JSON. The model is loaded and warmed exactly once; steady-state synthesis is
~0.2 s per chunk (~20x real-time), so a single warm worker easily serves the
pipeline without per-call model-load cost.

Protocol (one JSON object per line):

  ← (stdout, once at startup)  {"ready": true, "sample_rate": 24000, "model": "..."}
                          or   {"ready": false, "error": "..."}  then exit non-zero
  → (stdin, per request)       {"id": "r1", "text": "...", "voice": "af_heart",
                                "speed": 1.0, "lang_code": "a", "out_path": "/tmp/x.wav"}
  ← (stdout, per response)     {"id": "r1", "ok": true, "out_path": "/tmp/x.wav",
                                "duration_ms": 1234.5, "synth_ms": 210.0}
                          or   {"id": "r1", "ok": false, "error": "..."}

Design rules:
- The request loop NEVER dies on a per-request error; it reports ``ok:false`` and
  keeps serving. Only a fatal model-load failure or stdin EOF ends the process.
- stdout carries ONLY protocol JSON (one object per line, flushed). All human
  logging goes to stderr so it can't corrupt the channel.
"""

import sys
import os
import json
import time

# Reserve the REAL stdout for the protocol channel, then redirect the process
# stdout to stderr. mlx_audio (and spaCy/misaki) print progress chatter to
# stdout ("Creating new KokoroPipeline...", download bars); if that reached our
# channel it would corrupt the JSON line stream. After this swap, any library
# print() lands on stderr and only ``_emit`` writes protocol JSON.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr


def _log(msg: str) -> None:
    """Human-readable logging — stderr only (stdout is the protocol channel)."""
    sys.stderr.write(f"[kokoro_worker] {msg}\n")
    sys.stderr.flush()


def _emit(obj: dict) -> None:
    """Write one protocol JSON object to the reserved channel and flush."""
    _PROTOCOL_OUT.write(json.dumps(obj) + "\n")
    _PROTOCOL_OUT.flush()


def main() -> int:
    model_id = os.environ.get("KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")
    default_voice = os.environ.get("KOKORO_VOICE", "af_heart")
    default_lang = os.environ.get("KOKORO_LANG", "a")

    # ---- Load + warm the model once (the expensive, amortized cost) ----
    try:
        import numpy as np
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model
        from mlx_audio.audio_io import write as audio_write

        t0 = time.time()
        model = load_model(model_id)
        sample_rate = int(getattr(model, "sample_rate", 24000))
        # First synth pays a one-time language-pipeline (misaki/spaCy) init of
        # several seconds; do it now so the first *real* request is already warm.
        list(model.generate(text="ready", voice=default_voice, speed=1.0,
                            lang_code=default_lang))
        _log(f"model '{model_id}' loaded + warmed in {time.time() - t0:.1f}s "
             f"(sample_rate={sample_rate})")
    except Exception as e:  # fatal — cannot serve without a model
        _emit({"ready": False, "error": f"{type(e).__name__}: {e}"})
        _log(f"FATAL load failure: {e}")
        return 1

    _emit({"ready": True, "sample_rate": sample_rate, "model": model_id})

    # ---- Serve requests until stdin closes ----
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _emit({"id": None, "ok": False, "error": f"bad json: {e}"})
            continue

        req_id = req.get("id")
        try:
            text = req["text"]
            out_path = req["out_path"]
            voice = req.get("voice") or default_voice
            speed = float(req.get("speed", 1.0))
            # Kokoro's language code IS the voice-name prefix letter
            # (af->a American, bf->b British, ef->e Spanish, etc.). Deriving it
            # from the voice keeps G2P correct for any voice without per-voice
            # config; fall back to the request/default lang otherwise.
            lang_code = req.get("lang_code") or default_lang
            import re as _re
            if _re.match(r"^[a-z][fm]_", voice):
                lang_code = voice[0]

            t1 = time.time()
            segments = []
            for result in model.generate(text=text, voice=voice, speed=speed,
                                         lang_code=lang_code):
                audio = getattr(result, "audio", None)
                if audio is not None:
                    segments.append(np.asarray(audio).flatten())
            if not segments:
                raise RuntimeError("no audio produced")
            audio = segments[0] if len(segments) == 1 else np.concatenate(segments)

            # Atomic-ish write: synth to a temp sibling then rename, so the
            # playback stage never sees a half-written file.
            tmp_path = f"{out_path}.part"
            audio_write(tmp_path, mx.array(audio), sample_rate, format="wav")
            os.replace(tmp_path, out_path)

            synth_ms = (time.time() - t1) * 1000.0
            duration_ms = (audio.shape[0] / sample_rate) * 1000.0
            _emit({"id": req_id, "ok": True, "out_path": out_path,
                   "duration_ms": round(duration_ms, 1),
                   "synth_ms": round(synth_ms, 1)})
        except Exception as e:
            _emit({"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"})
            _log(f"request {req_id} failed: {e}")

    _log("stdin closed — exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
