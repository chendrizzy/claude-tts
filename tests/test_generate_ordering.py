"""GenerateStage ordered-yield head-of-line fix.

A chunk that fails synthesis (returns None / raises) must skip ONLY itself and
let the good TAIL chunks still flow. Before the fix, a None at a middle index
stalled `next_expected` forever, stranding every later chunk → the listener
heard the head then silence (a clean-sentence-boundary truncation) and the tail
was never logged. Sync via asyncio.run so it runs in the all-sync verify gate.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from daemon.pipeline.generate_stage import GenerateStage


def _stage(tmp_path):
    # min_free_bytes is irrelevant here — the disk guard lives inside
    # _generate_chunk, which we stub out entirely.
    return GenerateStage(cache_dir=str(tmp_path), min_free_bytes=1)


def _run(gs, n_chunks, fail_index):
    processed = SimpleNamespace(
        session_id="s1",
        request_id="r1",
        chunks=[f"chunk {i}" for i in range(n_chunks)],
    )

    async def fake_generate_chunk(chunk, session_id, request_id, chunk_index,
                                  total_chunks, voice, semaphore):
        if chunk_index == fail_index:
            return None                 # transient synth failure -> None segment
        return f"seg{chunk_index}"      # stand-in AudioSegment (truthy)

    gs._generate_chunk = fake_generate_chunk

    async def _collect():
        return [s async for s in gs.generate(processed, voice="v")]

    return asyncio.run(_collect())


def test_middle_chunk_failure_does_not_strand_tail(tmp_path):
    # chunk 1 fails -> 0, 2, 3 still play, in order (the head-of-line-stall bug).
    assert _run(_stage(tmp_path), n_chunks=4, fail_index=1) == ["seg0", "seg2", "seg3"]


def test_last_chunk_failure_keeps_head(tmp_path):
    assert _run(_stage(tmp_path), n_chunks=4, fail_index=3) == ["seg0", "seg1", "seg2"]


def test_first_chunk_failure_keeps_tail(tmp_path):
    assert _run(_stage(tmp_path), n_chunks=4, fail_index=0) == ["seg1", "seg2", "seg3"]


def test_all_success_yields_all_in_order(tmp_path):
    assert _run(_stage(tmp_path), n_chunks=4, fail_index=-1) == ["seg0", "seg1", "seg2", "seg3"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
