#!/usr/bin/env python3
"""Synthesize the README audio sample — the exact lines the demo GIF marks spoken.

Reuses the GIF's router replay (``demo_gif.classify_all``) so the clip can never
drift from the GIF: both derive from the same ContentRouter verdicts. Voiced with
edge-tts (the default engine) so the sample matches an out-of-the-box install.

    uv run --with pillow python scripts/demo_audio.py    # pillow: demo_gif imports it

Output: docs/media/sample.mp3
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_gif import ROOT, classify_all  # noqa: E402

import edge_tts  # noqa: E402

VOICE = "en-US-AvaNeural"  # the edge-tts default in config.example.json
OUT = ROOT / "docs" / "media" / "sample.mp3"


async def main() -> None:
    rows = await classify_all()
    lines = [r["spoken"] for r in rows if r["speak"]]
    # Two spaces between lines → edge-tts gives each its own breath, as the
    # daemon would when speaking them as separate utterances.
    text = "  ".join(lines)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    await edge_tts.Communicate(text, VOICE).save(str(OUT))
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({kb:.0f} KB)")
    print(f"  voiced ({VOICE}): {text}")


if __name__ == "__main__":
    asyncio.run(main())
