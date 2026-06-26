"""GenerateStage disk guard — the P0 fix for the RECURRING disk-full silent mute.

A periodic age-based cache sweep (cleanup_old_cache) already exists, but the disk
filled AGAIN (2026-06-25) because nothing checks free space *just before* writing
a synthesized chunk. This guard refuses to synthesize (and signals LOUDLY) when
the cache volume is nearly full, instead of letting the write fail silently and
muting TTS with no indication.

Sync via asyncio.run so it runs in the all-sync `make verify` gate.
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from daemon.pipeline.generate_stage import GenerateStage

MB = 1024 * 1024


class _FakeUsage:
    def __init__(self, free):
        self.free = free
        self.total = 10 ** 12
        self.used = self.total - free


def _stage(tmp_path, min_free):
    return GenerateStage(cache_dir=str(tmp_path), min_free_bytes=min_free)


def test_guard_allows_when_space_available(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _FakeUsage(free=2 * 1024 * MB))
    gs = _stage(tmp_path, min_free=200 * MB)
    assert asyncio.run(gs._ensure_disk_space("s1")) is True


def test_guard_blocks_and_signals_when_full(tmp_path, monkeypatch):
    # ~1 MB free, well under the 200 MB floor, and the empty cache can't reclaim.
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _FakeUsage(free=1 * MB))
    gs = _stage(tmp_path, min_free=200 * MB)
    fired = {}
    monkeypatch.setattr(gs, "_signal_disk_full",
                        lambda sid, free: fired.update(sid=sid, free=free))
    assert asyncio.run(gs._ensure_disk_space("s1")) is False
    assert fired.get("sid") == "s1"          # loud signal fired with the session


def test_guard_recovers_after_eviction(tmp_path, monkeypatch):
    # First check is low, but cleanup_old_cache "frees" space → second check passes.
    seq = iter([_FakeUsage(free=1 * MB), _FakeUsage(free=2 * 1024 * MB)])
    monkeypatch.setattr(shutil, "disk_usage", lambda p: next(seq))
    gs = _stage(tmp_path, min_free=200 * MB)
    fired = {}
    monkeypatch.setattr(gs, "_signal_disk_full",
                        lambda sid, free: fired.update(sid=sid))
    assert asyncio.run(gs._ensure_disk_space("s1")) is True
    assert "sid" not in fired                # recovered → no alert


def test_guard_never_raises_on_usage_error(tmp_path, monkeypatch):
    def boom(p):
        raise OSError("statvfs failed")
    monkeypatch.setattr(shutil, "disk_usage", boom)
    gs = _stage(tmp_path, min_free=200 * MB)
    # A guard bug must NEVER be the reason TTS breaks → degrade to "allow".
    assert asyncio.run(gs._ensure_disk_space("s1")) is True


if __name__ == "__main__":  # ponytail self-check
    import tempfile
    from pathlib import Path
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
