"""GenerateStage.cleanup_old_cache — the previously-dead sweep now wired into
the orchestrator's periodic loop (2026-06-21 disk-full outage: orphaned-session
WAVs grew /tmp/tts_audio_cache without bound until the disk filled, silently
muting ALL TTS engines).

Verifies the age policy: files older than max_age are deleted, fresh files are
kept. No network, no engine, no daemon.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.pipeline.generate_stage import GenerateStage


def test_cleanup_old_cache_deletes_old_keeps_fresh(tmp_path):
    cache = tmp_path / "tts_audio_cache"
    cache.mkdir()

    old = cache / "sess_old_0_abc.wav"
    fresh = cache / "sess_fresh_0_def.wav"
    old.write_bytes(b"RIFF")
    fresh.write_bytes(b"RIFF")

    # Age the old file 2h into the past; leave fresh at "now".
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    gs = GenerateStage(engine="edge-tts", cache_dir=str(cache))
    asyncio.run(gs.cleanup_old_cache(max_age_seconds=3600))

    assert not old.exists(), "file older than max_age should be swept"
    assert fresh.exists(), "fresh file must survive the sweep"


if __name__ == "__main__":  # ponytail self-check
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_cleanup_old_cache_deletes_old_keeps_fresh(Path(d))
    print("cache cleanup self-check OK")
