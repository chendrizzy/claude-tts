#!/usr/bin/env python3
"""Deterministic generator for the spoken-text golden corpus (cases.jsonl).

Each case is a contract: a RAW snippet (as Claude/tooling emits it) and what the
spoken rendering MUST and MUST-NOT contain after normalize_for_speech() +
ProcessStage cleaning. Seeded from the real markup leakage reproduced in
shadow.log (~23% of spoken output) and the 2026-06-03 diagnosis examples.

Run:  python3 tests/fixtures/spoken_corpus/_generate.py
Then: tests/test_spoken_render.py reads cases.jsonl as its source of truth.

Holding the cases as native Python here means backticks, box-drawing chars,
pipes and regex backslashes are escaped correctly by json.dumps — no hand-
editing of JSONL. Adding a case = add a dict below and re-run.
"""
from __future__ import annotations

import json
from pathlib import Path

BOX = "─"   # box-drawing horizontal — the literal '* Insight -----' offender
STAR = "★"  # black star

# Each case:
#   id          stable name
#   raw         input as emitted
#   require     literal substrings that MUST survive into spoken text
#   forbid      literal substrings that MUST NOT appear in spoken text
#   forbid_re   regex patterns that MUST NOT match anywhere in spoken text
#   note        why this case exists
CASES = [
    # ---- headers -------------------------------------------------------
    {"id": "atx_header_with_time", "raw": "## 06:47 Summary of changes",
     "require": ["06:47", "Summary of changes"], "forbid": ["##"], "forbid_re": [],
     "note": "ATX header marker dropped; time and text kept (06:47 must NOT be eaten)"},
    {"id": "atx_header_h3", "raw": "### Implementation details",
     "require": ["Implementation details"], "forbid": ["#"], "forbid_re": [],
     "note": "deeper header marker dropped"},

    # ---- emphasis ------------------------------------------------------
    {"id": "strong_stars", "raw": "This is **really** important",
     "require": ["really", "important"], "forbid": ["**"], "forbid_re": [],
     "note": "bold markers removed, words kept"},
    {"id": "strong_underscores", "raw": "value is __really critical__ now",
     "require": ["really critical", "now"], "forbid": ["__"], "forbid_re": [],
     "note": "multi-word __strong__ removed; single-word/dunder __x__ is left alone (see dunder_preserved)"},
    {"id": "italic_stars", "raw": "this is *emphasized* text",
     "require": ["emphasized", "text"], "forbid": ["*emphasized*"], "forbid_re": [],
     "note": "single-star emphasis unwrapped"},
    {"id": "strong_truncated", "raw": "the build **failed in",
     "require": ["the build", "failed in"], "forbid": ["**"], "forbid_re": [],
     "note": "unbalanced ** from a 200-char truncation must still clean (idempotent fixed point)"},

    # ---- inline code (the dangling-fragment regression) ----------------
    {"id": "inline_code_kept", "raw": "Run `pytest -q` now (3 matches)",
     "require": ["pytest -q", "3 matches"], "forbid": ["`"], "forbid_re": [],
     "note": "REGRESSION: old code deleted backtick CONTENTS -> 'Run  now (3 matches)'. Must KEEP."},
    {"id": "inline_code_dangling", "raw": "the `useRef`-based hook re-renders",
     "require": ["useRef-based hook"], "forbid": ["`"], "forbid_re": [],
     "note": "REGRESSION: old code produced 'the -based hook'. Must produce 'the useRef-based hook'."},

    # ---- lists ---------------------------------------------------------
    {"id": "bullet_dash", "raw": "- first item\n- second item",
     "require": ["first item", "second item"], "forbid": [], "forbid_re": [r"(?m)^\s*[-*+]\s"],
     "note": "leading bullet markers dropped at line start, text kept"},
    {"id": "bullet_star", "raw": "* alpha\n* beta",
     "require": ["alpha", "beta"], "forbid": [], "forbid_re": [r"(?m)^\s*\*\s"],
     "note": "star bullets dropped"},
    {"id": "numbered_list", "raw": "1. step one\n2. step two\n3) step three",
     "require": ["step one", "step two", "step three"], "forbid": [], "forbid_re": [r"(?m)^\s*\d+[.)]\s"],
     "note": "ordered-list markers (both . and )) dropped, step text kept"},
    {"id": "nested_quote_bullet", "raw": "> - deeply nested item",
     "require": ["deeply nested item"], "forbid": [], "forbid_re": [r"(?m)^\s*[>\-*+]\s"],
     "note": "IDEMPOTENCE-CRITICAL: stacked '> - ' markers must fully strip in one pass"},

    # ---- tables --------------------------------------------------------
    {"id": "table_full", "raw": "| Name | Count |\n|------|-------|\n| foo | 24 |",
     "require": ["Name", "Count", "foo", "24"], "forbid": ["|------", "-------", "| Name"], "forbid_re": [],
     "note": "separator row dropped; data cells joined; pipe delimiters removed"},

    # ---- rules / box-drawing (Claude's own insight blocks!) ------------
    {"id": "box_insight_block",
     "raw": STAR + " Insight " + BOX * 15 + "\nKey realization here\n" + BOX * 20,
     "require": ["Insight", "Key realization here"], "forbid": [STAR, BOX], "forbid_re": [],
     "note": "the literal star+box 'Insight' block: decorations removed, words kept; pure-rule line dropped"},
    {"id": "hr_dashes", "raw": "above the line\n\n---\n\nbelow the line",
     "require": ["above the line", "below the line"], "forbid": [], "forbid_re": [r"(?m)^\s*-{3,}\s*$"],
     "note": "horizontal rule line dropped, content on both sides kept"},
    {"id": "hr_equals_setext", "raw": "Title Text\n=====\nbody follows",
     "require": ["Title Text", "body follows"], "forbid": [], "forbid_re": [r"(?m)^=+\s*$"],
     "note": "setext underline rule dropped"},

    # ---- links / images ------------------------------------------------
    {"id": "link_inline", "raw": "See [the docs](https://example.com/x) for help",
     "require": ["See the docs for help"], "forbid": ["[", "](", "https://", "example.com"], "forbid_re": [],
     "note": "link text kept, URL+brackets removed"},
    {"id": "image_alt", "raw": "![architecture diagram](img/arch.png) shows the flow",
     "require": ["architecture diagram", "shows the flow"], "forbid": ["![", "](", ".png)"], "forbid_re": [],
     "note": "image alt text kept, URL removed"},

    # ---- diffstat / entities / strike ----------------------------------
    {"id": "diffstat_bar", "raw": "uv.lock | 24 +++",
     "require": ["uv dot lock"], "forbid": ["+++", "| 24"], "forbid_re": [],
     "note": "git diffstat ' | 24 +++' noise stripped"},
    {"id": "html_entities", "raw": "use &gt; to redirect and &amp; to background",
     "require": ["redirect", "background"], "forbid": ["&gt;", "&amp;"], "forbid_re": [],
     "note": "HTML entities decoded for speech"},
    {"id": "strikethrough", "raw": "~~deprecated~~ use the new API",
     "require": ["deprecated", "new API"], "forbid": ["~~"], "forbid_re": [],
     "note": "strike markers removed, content kept"},

    # ---- fenced code ---------------------------------------------------
    {"id": "code_fence_dropped",
     "raw": "Here is the fix:\n```python\nx = compute_value()\nreturn x\n```\nDone.",
     "require": ["the fix", "Done."], "forbid": ["```", "x = compute_value", "return x"], "forbid_re": [],
     "note": ("multi-line fenced code is unspeakable -> dropped; prose around it kept. "
              "require uses 'the fix' (not 'Here is the fix') because the end-to-end "
              "pipeline legitimately contracts 'Here is' -> \"Here's\".")},

    # ---- CRITICAL NEGATIVE CASES: must NOT corrupt legitimate content --
    {"id": "shell_pipe_preserved", "raw": "I ran grep foo | wc -l to count lines",
     "require": ["grep foo | wc -l"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a single shell pipe is NOT a table and must survive verbatim"},
    {"id": "shell_double_pipe_preserved", "raw": "cat a | grep b | wc",
     "require": ["cat a | grep b | wc"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: two inline pipes without leading/trailing | are a command, not a table row"},
    {"id": "snake_case_preserved", "raw": "the user_name_field and max_count are set",
     "require": ["user_name_field", "max_count"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: underscores inside identifiers must not be eaten by _emphasis_ handling"},
    {"id": "glob_preserved", "raw": "match *.py and *.txt files",
     "require": ["*.py", "*.txt"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: spaced single stars (globs) are not emphasis"},
    {"id": "math_preserved", "raw": "compute a * b * c carefully",
     "require": ["a * b * c"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: multiplication with spaced stars must survive"},
    # ---- peer-review regression guards (2026-06-03 adversarial review) ----
    {"id": "python_exponent_preserved", "raw": "the value 2**8 equals 256 and x**y grows",
     "require": ["2**8", "256", "x**y"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: ** between alphanumerics is exponentiation, NOT bold. Eating it turned 2**8 into 28 (wrong number)."},
    {"id": "dunder_preserved", "raw": "call __init__ then check __name__ == __main__ and __repr__",
     "require": ["__init__", "__name__", "__main__", "__repr__"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: Python dunders must survive; underscore-bold previously ate them (__init__ -> init)"},
    {"id": "kwargs_acceptable", "raw": "remember to pass **kwargs to the wrapper",
     "require": ["kwargs", "wrapper"], "forbid": [], "forbid_re": [],
     "note": "**kwargs may render as 'kwargs' (fine for speech); must NOT corrupt into a number"},
    {"id": "year_prefix_preserved", "raw": "2024. The year was productive overall",
     "require": ["2024", "The year was productive"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a sentence starting with a 4-digit year is not a list item (numbered marker is 1-2 digits)"},
    {"id": "big_number_prefix_preserved", "raw": "100. That is the maximum we allow",
     "require": ["100", "the maximum we allow"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: 3+ digit leading number is a value, not a list marker (require avoids 'That is' which the pipeline contracts)"},
    {"id": "diffstat_prose_preserved", "raw": "the magnitude | 5 + x | is bounded",
     "require": ["magnitude", "5 + x", "bounded"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: inline '| 5 +' is prose/math, not a diffstat tail (diffstat is end-of-line only)"},
    {"id": "trailing_pipe_shell_preserved", "raw": "echo done | tee log.txt |",
     "require": ["echo done | tee log dot txt"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a shell pipeline ending in '|' is not a markdown table row (needs BOTH leading and trailing pipe)"},
    {"id": "single_col_table_sep", "raw": "| Status |\n|--------|\n| OK |",
     "require": ["Status", "OK"], "forbid": ["--------", "|--------"], "forbid_re": [],
     "note": "1-column table separator must be dropped, not leaked as a row cell"},
    {"id": "idempotence_box_then_marker", "raw": "★ > Key insight here",
     "require": ["Key insight here"], "forbid": ["★", ">"], "forbid_re": [],
     "note": "IDEMPOTENCE: removing the leading box-char must not leave a blockquote '>' for a second pass"},

    # ---- code-artifact gibberish (R5: syntax that TTS spells out) -------
    # These reproduce the user's live complaints: "equals equals equals",
    # "parentheses comma 8g 4h pareldis hrz h h h" — programmatic syntax read
    # literally by the voice. The corpus was previously BLIND to all of these.
    {"id": "triple_equals", "raw": "check if a === b before continuing",
     "require": ["check if a", "b before continuing"], "forbid": ["===", "=="], "forbid_re": [],
     "note": "'===' is spoken 'equals equals equals' (user's #1 complaint). Operator runs collapse to a single '='."},
    {"id": "double_equals", "raw": "the guard verifies x == y at runtime",
     "require": ["the guard verifies x", "y at runtime"], "forbid": ["=="], "forbid_re": [],
     "note": "'==' -> 'equals equals'. Collapse to single '=' (spoken naturally as 'equals')."},
    {"id": "not_equals", "raw": "it fails when status != ready",
     "require": ["it fails when status", "ready"], "forbid": ["!="], "forbid_re": [],
     "note": "'!=' spoken 'exclamation equals' is gibberish; mapped to a word."},
    {"id": "logic_and_or", "raw": "run when ci && main || hotfix",
     "require": ["run when ci", "main", "hotfix"], "forbid": ["&&", "||"], "forbid_re": [],
     "note": "'&&'/'||' spoken char-by-char is noise; mapped to 'and'/'or'."},
    {"id": "arrows_mapped", "raw": "the rule maps key => value and a -> b",
     "require": ["the rule maps key", "value", "b"], "forbid": ["=>", "->"], "forbid_re": [],
     "note": "arrow operators read as 'equals greater than' / 'dash greater than'; mapped to 'to'."},
    {"id": "git_sha_short", "raw": "reverted in commit a1b2c3d cleanly",
     "require": ["reverted in commit", "cleanly"], "forbid": ["a1b2c3d"], "forbid_re": [],
     "note": "a git SHA (hex run with digits) is spelled out letter-by-letter; dropped, surrounding prose kept."},
    {"id": "git_sha_long", "raw": "HEAD now points at 9f8e7d6c5b4a3021 locally",
     "require": ["HEAD now points at", "locally"], "forbid": ["9f8e7d6c5b4a3021"], "forbid_re": [],
     "note": "full 16-char hash dropped"},
    {"id": "uuid_dropped", "raw": "session 550e8400-e29b-41d4-a716-446655440000 expired",
     "require": ["session", "expired"], "forbid": ["550e8400", "446655440000"], "forbid_re": [],
     "note": "UUID spelled segment-by-segment; dropped whole"},
    {"id": "uuid_truncated", "raw": "namespace DE8B1AAE-BC3B-498A-88A3-2FAF4 was used",
     "require": ["namespace", "was used"], "forbid": ["DE8B1AAE", "BC3B"], "forbid_re": [],
     "note": "REAL shadow.log case: a UUID cut by the 160-char excerpt cap still drops (dashed hex chain)"},
    {"id": "git_sha_after_dash", "raw": "checked out agent-a1b2c3d4e5 worktree cleanly",
     "require": ["checked out", "worktree cleanly"], "forbid": ["a1b2c3d4e5"], "forbid_re": [],
     "note": "REAL shadow.log case: 'agent-<sha>' worktree ids — hash after a '-' must drop (agent prefix kept)"},
    {"id": "git_sha_backtick_sentence_end", "raw": "Committed `d49215d`. Three bugs fixed.",
     "require": ["Committed", "Three bugs fixed."], "forbid": ["d49215d", "`"], "forbid_re": [],
     "note": "REAL shadow.log case: a short SHA in backticks at a sentence end — the trailing '.' must not shield it"},
    {"id": "hex_color_dropped", "raw": "set the banner color to #ff00ff today",
     "require": ["set the banner color to", "today"], "forbid": ["#ff00ff", "ff00ff"], "forbid_re": [],
     "note": "6/8-digit hex color dropped (but #42 and C# survive — see safety cases)"},
    {"id": "base64_blob_dropped", "raw": "the token aGVsbG8gd29ybGQxMjM0 then expired",
     "require": ["the token", "then expired"], "forbid": ["aGVsbG8"], "forbid_re": [],
     "note": "base64 blob spelled out letter-by-letter; dropped"},
    {"id": "diff_hunk_header", "raw": "@@ -1,5 +1,7 @@ adjusted the guard",
     "require": ["adjusted the guard"], "forbid": ["@@", "-1,5", "+1,7"], "forbid_re": [],
     "note": "unified-diff hunk header is pure syntax; dropped"},
    {"id": "single_letter_run", "raw": "the hash reads h h h h then stops",
     "require": ["the hash reads", "then stops"], "forbid": [], "forbid_re": [r"\bh h h\b"],
     "note": "TTS phoneticizing a blob yields lone-letter runs ('h h h'); collapsed away"},
    {"id": "empty_bracket_punct", "raw": "the tuple ( , , ) had no values",
     "require": ["the tuple", "had no values"], "forbid": ["( ,", ", )"], "forbid_re": [],
     "note": "after a blob is stripped from a tuple, the empty '( , , )' is 'parentheses comma' noise; removed"},
    {"id": "orphan_open_paren_comma", "raw": "the status (, ready to deploy",
     "require": ["the status", "ready to deploy"], "forbid": ["(,", "( ,"], "forbid_re": [],
     "note": "R5 live-path: a truncated extraction leaves '(,' which TTS reads 'parenthesis comma'; the orphan open bracket + dangling comma are stripped"},
    {"id": "orphan_open_paren_kept_content", "raw": "Duration 7m (476s) and (c) 2026 build (RED)",
     "require": ["(476s)", "(c)", "(RED)"], "forbid": [], "forbid_re": [],
     "note": "R5 SAFETY: content-bearing parens (a non-separator follows '(') survive — only '(' immediately before ',;:' is orphan residue"},
    {"id": "orphan_dangling_colon", "raw": "build finished : all green",
     "require": ["build finished", "all green"], "forbid_re": [r"\s:\s"], "forbid": [],
     "note": "R5 live-path: a solo ':' orphaned by truncation reads as 'colon'; a separator surrounded by spaces is swept (glued 'key: val' survives — see colon_glued_kept)"},
    {"id": "colon_glued_kept", "raw": "INFO: server started and schema_version: 0.1.0 ready",
     "require": ["INFO:", "schema_version: 0.1.0"], "forbid": [], "forbid_re": [],
     "note": "R5 SAFETY: a ':' glued to a preceding token (label/key) is NOT orphan punctuation and survives"},
    {"id": "ansi_codes_stripped", "raw": "Status: \x1b[32mOK\x1b[0m and \x1b[1;31mERROR\x1b[0m here",
     "require": ["Status", "OK", "ERROR", "here"], "forbid": ["[32m", "[0m", "[1;31m"], "forbid_re": [],
     "note": "ADVERSARIAL-REVIEW: terminal ANSI colour codes leaked as 'ESC bracket 32 m' gibberish; stripped"},
    {"id": "long_hex_blob_dropped", "raw": "the cache key deadbeefdeadbeef00 then expired",
     "require": ["the cache key", "then expired"], "forbid": ["deadbeefdeadbeef"], "forbid_re": [],
     "note": "a 16+ hex run WITH a hex letter is a hash/blob; dropped"},

    # ---- more code-artifact SAFETY (adversarial-review regressions) -----
    {"id": "long_digit_id_kept", "raw": "order 1234567890123456 shipped overnight",
     "require": ["1234567890123456"], "forbid": [], "forbid_re": [],
     "note": "ADVERSARIAL-REVIEW: a 16+ PURE-DIGIT run is a real number (order/account id), NOT a hash — must survive (was wrongly deleted)"},
    {"id": "long_account_number_kept", "raw": "account 12345678901234567890 is active",
     "require": ["12345678901234567890"], "forbid": [], "forbid_re": [],
     "note": "ADVERSARIAL-REVIEW: a 20-digit account number must survive"},

    # ---- code-artifact SAFETY (must NOT over-strip legit content) -------
    {"id": "single_equals_kept", "raw": "we set x = 5 in the config",
     "require": ["x = 5"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a SINGLE '=' reads naturally as 'equals'; only RUNS (==, ===) collapse"},
    {"id": "resolution_4k_kept", "raw": "export 4K video at 60fps now",
     "require": ["4K", "60fps"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: short alnum units (4K, 60fps) are not hashes"},
    {"id": "ram_units_kept", "raw": "the box has 8GB RAM and 16GB swap",
     "require": ["8GB", "16GB"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: 8GB/16GB are not hex hashes (G,B are non-hex) and stay"},
    {"id": "hex_word_kept", "raw": "the deadbeef sentinel value was set",
     "require": ["deadbeef"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a hex-letter WORD with no digits and <16 chars is real English; kept (hash rule needs letter+digit)"},
    {"id": "issue_number_kept", "raw": "see issue #42 and ticket #7 today",
     "require": ["#42", "#7"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: '#NN' issue refs survive (hex-color rule needs 6+ hex digits)"},
    {"id": "csharp_kept", "raw": "the service is written in C# and F#",
     "require": ["C#", "F#"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: single '#' on a language name survives"},
    {"id": "pure_digits_kept", "raw": "order 1234567 shipped overnight",
     "require": ["1234567"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a pure-digit number is read fine ('one million...'), not gibberish; hash rule needs a LETTER too"},
    {"id": "uppercase_initialism_kept", "raw": "it was made in the U S A proudly",
     "require": ["U S A"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: uppercase spaced initialisms survive; only lowercase lone-letter runs (blob residue) collapse"},
    {"id": "version_triplet_kept", "raw": "we upgraded to 1.2.3 in production",
     "require": ["1.2.3"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: dotted version numbers survive"},
    {"id": "date_dashes_kept", "raw": "the release on 2024-01-15 went smoothly",
     "require": ["2024-01-15"], "forbid": [], "forbid_re": [],
     "note": "SAFETY: a pure-numeric dashed date is NOT a hash (hash chain rule needs a hex letter); survives"},

    # ---- TASK B: ISO-8601 timestamps (digit-by-digit gibberish) ------------
    {"id": "iso_datetime_z_dropped", "raw": "build started at 2026-05-04T22:06:50Z then finished",
     "require": ["build started at", "then finished"],
     "forbid": ["2026-05-04T22:06:50Z", "2026-05-04", "22:06:50"], "forbid_re": [],
     "note": ("TASK B survivor 1: an ISO-8601 timestamp carrying a 'T' time and 'Z' zone is "
              "read digit-by-digit; dropped whole. A bare date '2024-01-15' (no T/Z) survives "
              "— see date_dashes_kept.")},
    {"id": "iso_datetime_no_z_dropped", "raw": "logged at 2026-05-04T22:06:50 during the run",
     "require": ["logged at", "during the run"],
     "forbid": ["2026-05-04T22:06:50", "2026-05-04"], "forbid_re": [],
     "note": "TASK B: ISO datetime with a 'T' time but no trailing 'Z' is still a timestamp; dropped"},
    {"id": "env_assign_dump_dropped",
     "raw": "PLAN_START_TIME=2026-05-04T22:06:50Z PLAN_START_EPOCH=1777932410",
     "require": [], "forbid": ["2026-05-04T22:06:50Z", "22:06:50"], "forbid_re": [],
     "note": ("TASK B survivor 1 (full): an env-var/timestamp assignment dump — the ISO timestamp "
              "is stripped by normalize; is_speakable then drops the whole ALL_CAPS_SNAKE= dump "
              "(see test_spoken_render is_speakable cases).")},

    # ---- realistic composite (everything at once) ----------------------
    {"id": "realistic_mixed",
     "raw": ("## Summary\n\nFixed the **race condition** in `queue_manager.py`:\n\n"
             "- preempt guard added\n- cascade capped at `YELLOW`\n\n"
             "See [the diff](http://x.com/d) for details."),
     "require": ["Summary", "race condition", "queue_manager dot py", "preempt guard added",
                 "cascade capped at YELLOW", "See the diff for details"],
     "forbid": ["##", "**", "`", "](", "http"], "forbid_re": [r"(?m)^\s*-\s"],
     "note": "end-to-end: every markdown construct in one realistic answer"},
]


def main() -> None:
    out = Path(__file__).parent / "cases.jsonl"
    seen = set()
    with out.open("w", encoding="utf-8") as fh:
        for c in CASES:
            assert c["id"] not in seen, f"duplicate id {c['id']}"
            seen.add(c["id"])
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"wrote {len(CASES)} cases -> {out}")


if __name__ == "__main__":
    main()
