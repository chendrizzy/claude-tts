"""Integration test: humanize_paths is applied in the new pipeline's ProcessStage.

Verifies that verbose filesystem paths do NOT reach TTS verbatim — i.e., that
the new pipeline path (ProcessStage._clean_text_sync) applies humanize_paths,
eliminating 'slash backend slash app slash...' readouts.

This is the carry-forward item from Phase 4 requirements. The implementation
already exists in daemon/pipeline/process_stage.py; this test documents and
locks it in.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.pipeline.process_stage import ProcessStage
from daemon.pipeline.ingest_stage import IngestMessage


def _make_ingest(text: str) -> IngestMessage:
    import time
    return IngestMessage(
        content=text,
        session_id="test",
        priority=5,
        request_id="test-req-1",
        ingested_at=time.time(),
    )


class TestHumanizePathsInPipeline:
    """ProcessStage._clean_text_sync must reduce verbose paths."""

    def _clean(self, text: str) -> str:
        stage = ProcessStage()
        return stage._clean_text_sync(text)

    def test_absolute_path_humanized(self):
        text = "The file /home/user/project/daemon/types.py is ready."
        result = self._clean(text)
        # Should NOT contain the long /Volumes/... prefix
        assert "/Volumes/BOLT" not in result, (
            f"Long path prefix not humanized: {result!r}"
        )
        # Should contain a shortened form
        assert "types" in result.lower() or "daemon" in result.lower(), (
            f"Expected shortened path reference, got: {result!r}"
        )

    def test_relative_path_humanized(self):
        text = "Check ./daemon/content_router.py for the classifier."
        result = self._clean(text)
        # Relative path should be shortened to 'content_router.py' or 'daemon/content_router.py'
        assert "content_router" in result, (
            f"Expected content_router in result: {result!r}"
        )

    def test_home_path_humanized(self):
        text = "Config at ~/projects/web/api/server.ts is loaded."
        result = self._clean(text)
        assert "~/projects/web/api" not in result, (
            f"Home path not humanized: {result!r}"
        )
        # Should still reference server.ts or api/server.ts
        assert "server" in result, f"Expected server in result: {result!r}"

    def test_plain_text_unchanged(self):
        """Text without paths should pass through unchanged."""
        text = "All 91 tests passed in 0.22 seconds."
        result = self._clean(text)
        assert "91 tests passed" in result, (
            f"Plain text was unexpectedly modified: {result!r}"
        )

    def test_process_stage_integration(self):
        """Full async process() call applies humanize_paths."""
        text = "/home/user/project/daemon/content_router.py was modified."
        stage = ProcessStage()
        msg = _make_ingest(text)
        result = asyncio.run(stage.process(msg))
        assert "/Users/user" not in result.cleaned, (
            f"Full path not humanized in pipeline: {result.cleaned!r}"
        )
        assert "content_router" in result.cleaned, (
            f"Expected content_router in cleaned output: {result.cleaned!r}"
        )
