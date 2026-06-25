#!/usr/bin/env python3
"""Render the README demo GIF by replaying real events through the real router.

This is a documentation artifact generator, not part of the daemon. It feeds a
short, hand-picked *sequence* of events from ``tests/fixtures/event_corpus.jsonl``
through the actual :class:`ContentRouter` (no LLM — the deterministic floor) and
draws each verdict the way the daemon logs it: SPEAK with the spoken line, or a
dimmed "quiet" for the noise it drops. Because the verdicts come from the live
classifier, the GIF can never drift from behaviour — the same corpus gates CI.

Run (Pillow is pulled ephemerally, never added to the daemon's deps):

    uv run --with pillow python scripts/demo_gif.py

Output: docs/media/demo.gif
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from daemon.content_router import ContentRouter  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "event_corpus.jsonl"
OUT = ROOT / "docs" / "media" / "demo.gif"

# The narrative: one believable session. Each entry names a corpus case `id` and
# the label to show for it (the real tool + argument from that event). Verdicts
# and spoken text are NOT written here — the router decides them at render time.
SCRIPT: list[tuple[str, str]] = [
    ("noise_read_success", "Read   foo.py"),
    ("status_pytest_pass_fail", "Bash   pytest"),
    ("err_bash_stderr_long", "Bash   cat /nonexistent"),
    ("noise_edit_success", "Edit   foo.py"),
    ("status_pytest_all_pass", "Bash   pytest -q"),
    ("final_short_answer", "Stop   final answer"),
]

# GitHub-dark palette.
BG = (13, 17, 23)
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
DIM = (110, 118, 129)
FG = (201, 209, 217)
WHITE = (240, 246, 252)
CAT_COLORS = {
    "error": (248, 113, 113),
    "status": (56, 189, 248),
    "final_answer": (74, 222, 128),
    "insight": (192, 132, 252),
}

W, H = 1040, 496
MARGIN = 36
TITLE_H = 52
ROW_H = 50
X_TIME = MARGIN + 24
X_LABEL = X_TIME + 104
X_VERDICT = X_LABEL + 344  # room for the widest label ("Bash  cat /nonexistent")
X_SPOKEN = X_VERDICT + 132


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["/System/Library/Fonts/SFNSMono.ttf"] if False else []
    ) + [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/Library/Fonts/Menlo.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


F = _font(23)
FB = _font(23)
FSMALL = _font(18)
FTITLE = _font(20)


def load_events() -> list[dict]:
    by_id = {}
    for line in CORPUS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        by_id[rec["name"]] = rec["event"]
    return by_id


async def classify_all() -> list[dict]:
    """Run each scripted event through the real router; collect verdicts."""
    by_id = load_events()
    router = ContentRouter(config={}, provider=None)  # provider=None → no LLM
    rows = []
    for case_id, label in SCRIPT:
        event = by_id[case_id]
        decision = await router.classify_event(event)
        spoken = ""
        if decision.should_speak:
            spoken = " ".join((decision.content or "").split())
            if len(spoken) > 72:  # keep short lines whole; only cap runaway content
                spoken = spoken[:71].rstrip() + "…"
        rows.append({
            "label": label,
            "speak": decision.should_speak,
            "category": decision.category.value if decision.category else None,
            "spoken": spoken,
        })
    return rows


def draw_base() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # Panel
    d.rounded_rectangle([MARGIN, MARGIN, W - MARGIN, H - MARGIN],
                        radius=12, fill=PANEL, outline=BORDER, width=1)
    # Title bar
    bar_y = MARGIN + TITLE_H
    d.line([MARGIN + 1, bar_y, W - MARGIN - 1, bar_y], fill=BORDER, width=1)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = MARGIN + 24 + i * 22
        cy = MARGIN + TITLE_H // 2
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=c)
    d.text((MARGIN + 104, MARGIN + 16), "claude-tts — what your agent says out loud",
           font=FTITLE, fill=DIM)
    return img


def render_frame(rows: list[dict], upto: int, highlight: int) -> Image.Image:
    img = draw_base()
    d = ImageDraw.Draw(img)
    top = MARGIN + TITLE_H + 22
    for i in range(upto + 1):
        r = rows[i]
        y = top + i * ROW_H
        is_new = (i == highlight)
        # left accent bar on the row that just landed
        if is_new and r["speak"]:
            cat_c = CAT_COLORS.get(r["category"], FG)
            d.rectangle([MARGIN + 1, y - 4, MARGIN + 4, y + 26], fill=cat_c)
        d.text((X_TIME, y), f"14:22:{i*3+1:02d}", font=FSMALL, fill=DIM)
        d.text((X_LABEL, y), r["label"], font=F, fill=FG if r["speak"] else DIM)
        if r["speak"]:
            cat_c = CAT_COLORS.get(r["category"], FG)
            d.text((X_VERDICT, y), "● SPEAK", font=FB, fill=cat_c)
            d.text((X_SPOKEN, y), f"“{r['spoken']}”", font=F,
                   fill=WHITE if is_new else FG)
            # category tag, right-aligned-ish under spoken
            d.text((X_VERDICT, y + 24), r["category"], font=FSMALL, fill=cat_c)
        else:
            d.text((X_VERDICT, y), "· quiet", font=F, fill=DIM)
    # Footer caption
    cap = "Read / Edit / dup noise → silent.   Tests, errors, the final answer → spoken."
    d.text((X_TIME, H - MARGIN - 30), cap, font=FSMALL, fill=DIM)
    return img


def build_gif(rows: list[dict]) -> None:
    # Size the canvas to the longest spoken line so nothing clips the panel.
    global W
    spoken_px = max(
        (F.getlength(f"“{r['spoken']}”") for r in rows if r["speak"]),
        default=0,
    )
    W = int(X_SPOKEN + spoken_px + 28 + MARGIN)

    frames: list[Image.Image] = []
    durations: list[int] = []
    # Opening hold on the empty-ish panel (first row visible briefly).
    for i in range(len(rows)):
        frames.append(render_frame(rows, upto=i, highlight=i))
        durations.append(950)
    # Settle frame (nothing highlighted), held long, before the loop restarts.
    frames.append(render_frame(rows, upto=len(rows) - 1, highlight=-1))
    durations.append(3200)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Quantize to a shared adaptive palette → small, crisp GIF.
    pal = frames[-1].convert("P", palette=Image.ADAPTIVE, colors=128)
    qframes = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]
    qframes[0].save(
        OUT, save_all=True, append_images=qframes[1:], duration=durations,
        loop=0, optimize=True, disposal=2,
    )
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({len(frames)} frames, {kb:.0f} KB)")
    # Small preview of the final frame for quick visual review (not committed).
    preview = OUT.with_name("_preview.png")
    frames[-1].resize((W // 2, H // 2)).save(preview)
    print(f"wrote {preview}  (preview, gitignored)")
    for r in rows:
        verdict = f"SPEAK [{r['category']}] “{r['spoken']}”" if r["speak"] else "quiet"
        print(f"  {r['label']:<26} {verdict}")


if __name__ == "__main__":
    rows = asyncio.run(classify_all())
    build_gif(rows)
