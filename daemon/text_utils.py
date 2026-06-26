"""Shared text-processing utilities applied right before TTS synthesis.

The single live chokepoint is `pipeline/process_stage.py::_clean_text_sync`
(every spoken utterance — raw, summarized, condensed, fallback — routes through
it before chunking/synthesis). `ollama_summarizer._rule_based_fallback` also
calls `normalize_for_speech` before truncating. The legacy `speak_text` path was
deleted in Phase 3; `tts_daemon.clean_text_for_speech` is dead code and must not
be relied on. Any utility added here hits every spoken utterance.
"""
from __future__ import annotations

import gzip
import html
import os
import re

# Match absolute paths, home-relative, and relative-with-./ paths that have
# at least 2 components. Tight enough to ignore single-segment "/tmp" paths
# (which read fine) and not eat URLs (no "://").
PATH_LIKE_RE = re.compile(
    r"""
    (?<![:\w/])                      # not preceded by ':', word char, or '/'
                                     # (the latter excludes the second '/' in URL '://')
    (?:
        /[\w.\-+%@]+(?:/[\w.\-+%@]+){1,}/?    # absolute: /a/b or /a/b/c/...
        | ~/(?:[\w.\-+%@]+/?)+                # home-relative: ~/...
        | \.{1,2}/(?:[\w.\-+%@]+/?)+          # ./ or ../ relative
    )
    """,
    re.VERBOSE,
)

# Path segments to drop from the SPOKEN form. Keeps cleanup deterministic
# without eliminating legitimate references.
_NOISE_PREFIXES = {
    # Absolute mount/home prefixes (always drop with the next segment too —
    # the next segment is usually a username or volume label that's noise).
    "Users", "Volumes", "home", "private", "var",
}

# Common dotted/hidden directories that aren't useful when spoken aloud.
# We strip these even when they appear mid-path.
_NOISE_SEGMENTS = {
    ".git", ".claude", ".venv", ".env", ".cache", ".pytest_cache",
    ".vscode", ".idea", ".tox", ".mypy_cache", ".ruff_cache",
    "node_modules", "__pycache__", "dist", "build", "target",
    "site-packages",
}


def _humanize_one_path(raw: str) -> str:
    """Reduce a single path token to a concise spoken form.

    Strategy (in order):
      * Strip leading mount/home/private noise (/Users/X/, /Volumes/X/, etc.)
      * Drop hidden/build/cache directories from the middle
      * Keep at most the last 2 meaningful segments — speak the deepest ones
      * Preserve file extensions on the basename so listeners can identify
        what kind of file it is (.py, .rs, etc.)

    Examples:
        "/Volumes/DISK/GitHub/music/example"  → "example"
        "/home/user/project/daemon/tts_types.py" → "daemon/tts_types.py"
        "src/foo/bar.py"                                → "bar.py"
        "~/projects/web/api/server.ts"                  → "api/server.ts"
        "./tests/fixtures/event_corpus.jsonl"           → "fixtures/event_corpus.jsonl"
    """
    if not raw:
        return raw
    # Normalize: strip leading ~, leading ./ or ../
    path = raw
    had_home = path.startswith("~/")
    if had_home:
        path = path[2:]
    if path.startswith("./") or path.startswith("../"):
        path = path[path.index("/") + 1:]

    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return raw

    # Drop noise prefix + the username/volume that follows it.
    while len(parts) >= 2 and parts[0] in _NOISE_PREFIXES:
        parts = parts[2:]

    # Drop hidden/build dirs from the middle (keep them only if they're the
    # entire remaining path — sometimes someone genuinely refers to ".git").
    if len(parts) > 1:
        parts = [p for p in parts if p not in _NOISE_SEGMENTS] or parts

    # Cap at last 2 meaningful segments.
    if len(parts) > 2:
        parts = parts[-2:]

    if not parts:
        return raw

    return "/".join(parts)


def humanize_paths(text: str) -> str:
    """Replace verbose filesystem paths in `text` with concise spoken forms.

    Conservative: only rewrites tokens that match PATH_LIKE_RE. Single-segment
    paths like "/tmp" are left alone (they read fine). URLs are left alone
    (the negative lookbehind for "://" guards against eating them).

    Pure function — no side effects, deterministic, ~50µs per call. Safe to
    call on every TTS-bound utterance.
    """
    if not text or "/" not in text:
        return text
    return PATH_LIKE_RE.sub(lambda m: _humanize_one_path(m.group(0)), text)


# ===========================================================================
# Markdown -> speech normalization.
#
# normalize_for_speech() destructures markdown/markup so the TTS voice never
# reads `**`, `##`, list markers, table pipes, `[links](url)`, blockquotes,
# horizontal rules, box-drawing, diffstats, or HTML entities. It is applied at
# the single ProcessStage chokepoint (process_stage._clean_text_sync) through
# which every spoken utterance passes -- including summarizer output, queue
# condensations, and fallback truncations that re-enter the cleaner.
#
# Three invariants, all fixture-tested in tests/test_spoken_render.py:
#   1. PURE      - no side effects, deterministic.
#   2. IDEMPOTENT- normalize(normalize(x)) == normalize(x). Required because
#                  condensed/summarized content is normalized more than once.
#   3. SAFE      - never corrupts legitimate speech: shell pipes (`a | b`),
#                  snake_case identifiers, and globs/`*.py` survive verbatim.
# ===========================================================================

# --- fenced + inline code -------------------------------------------------
# Fenced blocks are multi-line source -> unspeakable, dropped entirely.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# Unbalanced fence remnant left by a mid-block truncation -> drop to EOL.
_FENCE_OPEN_RE = re.compile(r"```[^\n]*")
# Inline code: KEEP the contents, drop only the backticks. The pre-fix bug
# deleted the contents ("the `useRef`-based hook" -> "the -based hook").
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# --- links / images -------------------------------------------------------
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")          # ![alt](url) -> alt
_LINK_INLINE_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)?")    # [text](url) or truncated [text](url… -> text
_LINK_REF_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")       # [text][ref]  -> text
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")          # <http..> -> http..

# --- emphasis / strong / strike (paired only) -----------------------------
# Guards (?<![\w*]) / (?![\w*]) and the non-space lookarounds keep these from
# matching snake_case underscores, spaced globs (`*.py`) or multiplication.
# Alnum guards so a bold span can't span two exponentiations (2**8 ... x**y was
# matched as one '**8 ... x**' bold and eaten). Real bold is flanked by
# whitespace/punctuation, not alphanumerics.
_STRONG_STAR_RE = re.compile(r"(?<![A-Za-z0-9])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![A-Za-z0-9])")
# Underscore-bold ONLY when the span is multi-word AND underscore-free, so it
# can never eat Python dunders (__init__, __name__) or snake_case — those have
# no internal space or contain underscores. Claude rarely emits __bold__ anyway.
_STRONG_US_RE = re.compile(r"__(?=\S)([^_\n]*?\s[^_\n]*?)(?<=\S)__")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_EM_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_EM_US_RE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")

# --- diffstat / decorations -----------------------------------------------
# git diffstat tail: " | 24 +++" — anchored to END OF LINE so it only eats a real
# trailing "| NN +++/---" and not inline prose like "the magnitude | 5 + x".
_DIFFSTAT_RE = re.compile(r"\s*\|\s*\d+\s*[-+]+\s*$")
# box-drawing, block elements, misc-symbol stars/bullets -> spoken as nothing.
_BOX_RE = re.compile(r"[─-╿▀-▟☀-⛿•·]")

# --- code / programmatic-syntax artifacts (R5) ----------------------------
# These are NOT markdown but are read literally by the voice as gibberish:
# "===" -> "equals equals equals", a git SHA spelled letter-by-letter, a UUID,
# a base64 blob, lone-letter runs ("h h h") left by a mangled token. Every rule
# below is word/space-bounded so legitimate speech survives untouched: a single
# '=' ("x = 5"), units (4K, 8GB), issue refs (#42), languages (C#, F#), dotted
# versions (1.2.3), pure numbers (1234567), hex-letter WORDS (deadbeef), and
# uppercase initialisms (U S A) are all preserved (proven by safety fixtures in
# tests/fixtures/spoken_corpus/_generate.py).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")                 # CSI/SGR colour & cursor codes
_DIFF_HUNK_RE = re.compile(r"@@[^@\n]*@@")                       # @@ -1,5 +1,7 @@
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Hex colour / hash after '#': 6 or 8 hex digits. '#42'/'#abc'/'C#' survive
# (they have fewer than 6 hex digits).
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b")
# ISO-8601 datetime carrying a 'T' time separator and/or a trailing 'Z' zone
# (e.g. "2026-05-04T22:06:50Z", "2026-05-04T22:06:50", "22:06:50Z"). A voice
# reads these digit-by-digit as gibberish ("two zero two six dash..."). Dropped
# whole. The 'T<digits>:' / trailing 'Z' is what distinguishes a TIMESTAMP from
# a bare calendar date "2024-01-15" (date_dashes_kept) — that has no T-time/Z
# and must survive untouched.
_ISO_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?\b"  # date + T-time (+Z)
    r"|\b\d{2}:\d{2}:\d{2}Z\b"                                   # bare HH:MM:SSZ
)
# Dashed hex chain: a full OR truncated UUID, or a "agent-<sha>"-style id, or a
# "session=c148-49e8-45eb" debug fragment. Dropped by the callback ONLY when it
# carries a hex letter (a-f), so a pure-numeric date "2024-01-15" survives.
_HASH_CHAIN_RE = re.compile(r"\b[0-9a-fA-F]{4,}(?:-[0-9a-fA-F]{2,}){1,}")
# A standalone hex run; dropped only by the callback when it is a real hash
# (has BOTH a letter and a digit). The lookbehind allows a leading '-' or '/'
# so a worktree id ("agent-a77f8abb") or a path tail ("/d0d7ff29") is caught,
# but blocks word-chars and dots so versions ("1.2.3") and identifiers survive.
# Lookahead is (?![0-9a-fA-F]): a hex token only extends via MORE hex, so a
# trailing sentence period ("Committed d49215d.") no longer shields the SHA,
# while a leading word-char/dot still protects "version1a2b" and "config.a1b2".
_HEX_TOKEN_RE = re.compile(r"(?<![\w.])[0-9a-fA-F]{7,40}(?![0-9a-fA-F])")
_HEX_LONG_RE = re.compile(r"(?<![\w.])[0-9a-fA-F]{16,}(?![0-9a-fA-F])")
# Git revision range "sha1..sha2" (e46ca57..3b4b4f1). The single-token rule above
# has a (?<![\w.]) lookbehind that protects dotted identifiers ("config.a1b2"),
# which ALSO shields the SECOND sha in a range (it sits right after the '..'),
# leaking it to speech. Match the range as a unit BEFORE the single-token pass and
# drop it when it really is a pair of hashes (pure-numeric ranges survive).
_HEX_RANGE_RE = re.compile(r"(?<![\w.])[0-9a-fA-F]{7,40}\.\.[0-9a-fA-F]{7,40}(?![0-9a-fA-F])")
# base64-ish blob (callback-gated so long lowercase words / camelCase survive).
_B64_RE = re.compile(r"(?<![\w/])[A-Za-z0-9+/]{16,}={0,2}(?![\w/])")
# Operator runs with no natural single-symbol reading.
_NEQ_RE = re.compile(r"!==?")                                    # != , !==
_ARROW_RE = re.compile(r"=>|->")                                 # => , ->
_EQ_RUN_RE = re.compile(r"={2,}")                               # == , === -> '='
# A lone lowercase letter (not the words 'a'/'i') repeated >=3x — the residue a
# voice engine leaves after phoneticizing a blob ("h h h h"). Different letters
# ("x y z") are NOT touched, so coordinates/initialisms survive.
_LONE_LETTER_RUN_RE = re.compile(r"(?:(?<=\s)|^)([b-hj-z])(?: \1){2,}(?=\s|$)")
# An empty / punctuation-only bracket left after a blob is stripped: "( , , )".
_EMPTY_BRACKET_RE = re.compile(r"[(\[{][\s,;:]*[)\]}]")
# Orphan OPEN punctuation left when extraction truncates after an opening
# bracket and ate its contents: "(," / "( ," / a lone "(" with no close on the
# fragment reads as "parenthesis comma". Strip an open bracket that is
# IMMEDIATELY followed by a separator (',' ';' ':') with no real token between —
# that bracket can never have matching content. Content-bearing parens
# ("(476s)", "(+)", "(c)", "(RED)") are untouched (a non-separator follows '(').
_ORPHAN_OPEN_RE = re.compile(r"[(\[{]\s*(?=[,;:])")
# A standalone/orphan separator left dangling between spaces or at the line
# edges: " , " or trailing " :" with no token it belongs to. Only a SOLO
# separator surrounded by whitespace/edges is swept, so "a, b" and "key: val"
# (separator glued to a token) survive.
_ORPHAN_SEP_RE = re.compile(r"(?:(?<=\s)|^)[,;:]+(?=\s|$)")
# Bracket residue left after a hash/sha INSIDE it was stripped: "[main ]",
# "[worktree-agent- ]" (git branch+sha commit lines). A trailing space before
# the close bracket is the tell that a token was removed. Keep the inner word,
# drop the brackets so TTS doesn't read "bracket main bracket". 2026-06-19
# live-path fix (shadow.log mining: 315 orphan-punct residues, this class).
_ORPHAN_BRACKET_RE = re.compile(r"[(\[{]\s*([\w./-]*?)\s+[)\]}]")


def _looks_like_hash(tok: str) -> bool:
    """A hex token is a real hash (vs. a word like 'deadbeef' or a number like
    '1234567') only if it mixes letters AND digits."""
    has_alpha = any(c in "abcdefABCDEF" for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    return has_alpha and has_digit


def _looks_like_hex_blob(tok: str) -> bool:
    """A 16+ hex run is a hash ONLY if it contains a hex LETTER (a-f). A pure-
    digit run is just a long number (order id, account #, timestamp) and must be
    KEPT — a TTS voice reads a number fine; deleting it loses real data."""
    return any(c in "abcdefABCDEF" for c in tok)


def _looks_like_b64(tok: str) -> bool:
    """Non-lexical base64-ish blob: a base64 symbol (+/=) or a digit+letter mix.
    Long pure-lowercase words and digit-free camelCase identifiers are kept."""
    core = tok.rstrip("=")
    if len(core) < 16:
        return False
    has_sym = ("+" in tok) or ("/" in tok) or tok.endswith("=")
    has_digit = any(c.isdigit() for c in core)
    has_letter = any(c.isalpha() for c in core)
    return has_sym or (has_digit and has_letter)


def _strip_code_artifacts(line: str) -> str:
    """Remove programmatic syntax a TTS voice would spell out as gibberish.

    Order matters: structural blobs (hunks, UUIDs, hashes, base64) are removed
    before operator runs are mapped to words, before lone-letter/empty-bracket
    residue is swept. Pure + idempotent (every output maps to itself)."""
    line = _ANSI_RE.sub("", line)            # terminal colour/cursor codes -> gone
    line = _DIFF_HUNK_RE.sub(" ", line)
    # ISO-8601 timestamps ("2026-05-04T22:06:50Z") before the hash-chain rule, so
    # the pure-digit date half isn't left behind. Bare dates "2024-01-15" lack
    # the T-time/Z and are untouched (date_dashes_kept).
    line = _ISO_DATETIME_RE.sub(" ", line)
    line = _UUID_RE.sub(" ", line)
    # Truncated UUIDs / agent-id fragments (dashed hex chains); dates survive.
    line = _HASH_CHAIN_RE.sub(lambda m: " " if _looks_like_hash(m.group(0)) else m.group(0), line)
    line = _HEX_COLOR_RE.sub(" ", line)
    # 16+ hex run: drop only if it has a hex LETTER (a real blob); a pure-digit
    # run is a long NUMBER (order/account id) and is kept.
    line = _HEX_LONG_RE.sub(lambda m: " " if _looks_like_hex_blob(m.group(0)) else m.group(0), line)
    # Git "sha1..sha2" range: drop the whole pair before the single-token pass,
    # whose dotted-identifier lookbehind would otherwise shield the second sha.
    line = _HEX_RANGE_RE.sub(
        lambda m: " " if _looks_like_hash(m.group(0).replace(".", "")) else m.group(0),
        line,
    )
    line = _HEX_TOKEN_RE.sub(lambda m: " " if _looks_like_hash(m.group(0)) else m.group(0), line)
    line = _B64_RE.sub(lambda m: " " if _looks_like_b64(m.group(0)) else m.group(0), line)
    line = _ARROW_RE.sub(" to ", line)
    line = _NEQ_RE.sub(" not equal ", line)
    line = line.replace("&&", " and ").replace("||", " or ")
    line = _EQ_RUN_RE.sub("=", line)        # ==, === collapse to a single '='
    line = _LONE_LETTER_RUN_RE.sub(" ", line)
    line = _ORPHAN_BRACKET_RE.sub(lambda m: " " + m.group(1) + " ", line)
    # Orphan OPEN punctuation: strip a "(" that immediately precedes a separator
    # (its contents were truncated away) — "(," reads as "parenthesis comma".
    # Runs BEFORE the empty-bracket sweep and the orphan-separator sweep so the
    # exposed lone separator is then removed below.
    line = _ORPHAN_OPEN_RE.sub(" ", line)
    line = _EMPTY_BRACKET_RE.sub(" ", line)
    # Lone dangling separators (", " / " :" with no token) left by truncation.
    line = _ORPHAN_SEP_RE.sub(" ", line)
    return line

# --- line-level block markers (stripped from the START of a line) ---------
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}[ \t]*")            # "## Title" -> "Title"
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>[ \t]?")             # "> quote"  -> "quote"
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+")              # "- item"   -> "item"
# Ordered-list marker: 1-2 digits only, so a sentence starting with a year
# ("2024. The year...") or a large value ("100. ...") keeps its number; real
# list markers are 1-99.
_NUMBER_RE = re.compile(r"^[ \t]*\d{1,2}[.)][ \t]+")        # "1. step"  -> "step"
# A line that is ENTIRELY a horizontal rule / setext underline -> dropped.
_RULE_LINE_RE = re.compile(r"^[ \t]*([-=_*#~]|─|—|–){3,}[ \t]*$")
# A markdown table separator row: |---| or |---|:--:| (1+ columns) -> dropped.
# Requires a leading pipe + at least one dash-cell, so a single-column
# separator "|--------|" is caught (it previously leaked as a "cell").
_TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*$"
)


def _strip_line_prefixes(line: str) -> str:
    """Strip leading header/blockquote/list markers, looping until stable so a
    stacked prefix like '> - item' fully resolves in one normalize() call
    (idempotence)."""
    prev = None
    while prev != line:
        prev = line
        line = _HEADER_RE.sub("", line)
        line = _BLOCKQUOTE_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        line = _NUMBER_RE.sub("", line)
    return line


def _strip_inline_markup(line: str) -> str:
    """Remove inline markdown from a single line, preserving content."""
    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_INLINE_RE.sub(r"\1", line)
    line = _LINK_REF_RE.sub(r"\1", line)
    line = _AUTOLINK_RE.sub(r"\1", line)
    line = _INLINE_CODE_RE.sub(r"\1", line)          # keep contents!
    # Orphan backticks left by a mid-`code` truncation (e.g. a 200-char
    # fallback cut) are never speakable -> drop them. Idempotent.
    line = line.replace("`", "")
    line = _STRONG_STAR_RE.sub(r"\1", line)
    line = _STRONG_US_RE.sub(r"\1", line)
    line = _STRIKE_RE.sub(r"\1", line)
    line = _EM_STAR_RE.sub(r"\1", line)
    line = _EM_US_RE.sub(r"\1", line)
    # Strip orphaned bold markers, but NEVER when '**' sits between two
    # alphanumerics — that is exponentiation/identifier context (2**8, x**y),
    # not markup. Eating it would silently turn 2**8 into 28 (wrong number).
    line = re.sub(r"(?<![A-Za-z0-9])\*\*|\*\*(?![A-Za-z0-9])", "", line)
    # Runs of 2+ '#' anywhere are markdown header markers (a header can land
    # mid-line after content is concatenated/truncated). Single '#' is left
    # intact so "C#", "F#" and "#42" survive. Idempotent.
    line = re.sub(r"#{2,} ?", " ", line)
    line = _DIFFSTAT_RE.sub("", line)
    line = _BOX_RE.sub(" ", line)
    # Code/programmatic-syntax artifacts (hashes, operators, blobs) -> speech.
    line = _strip_code_artifacts(line)
    return line


# A "real word" = 2+ letters, so a bare "OK"/"UP" status survives; the ratio
# guard below still drops number/symbol dumps that merely contain a stray word.
_SPEAKABLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")

# ---------------------------------------------------------------------------
# Semantic (dictionary-word-ratio) speakability gate.
#
# The structural gate above keeps a line that has SOME letters and isn't
# number-dominated. It still leaks alpha-heavy gibberish a listener can't use:
# `ls -l` output (ErrorBanner.tsx … user … wheel), ps-output rows, and
# agent-id / sha dumps that survive normalization because they're mostly
# letters. The semantic gate asks a different question: are the WORD tokens
# actually words? If a multi-token line has ZERO real words (or a vowelless
# non-acronym token dominates), it is gibberish and dropped.
#
# Deliberately conservative to protect recall: a SINGLE unknown token inside
# otherwise-real prose is always kept. We only drop when the text is
# essentially pure non-vocabulary.
# ---------------------------------------------------------------------------

# Tech acronyms / units / abbreviations that are NOT in /usr/share/dict/words
# but ARE legitimate spoken dev vocab. Lowercased membership test.
_TECH_ALLOWLIST = frozenset({
    # package managers / tooling
    "npm", "pnpm", "yarn", "bun", "uv", "pip", "pipx", "poetry", "cargo",
    "gradle", "maven", "tox", "pytest", "jest", "vitest", "eslint", "prettier",
    "ruff", "mypy", "tsc", "ffmpeg", "ffprobe",
    # protocols / formats / langs
    "sql", "oauth", "oauth2", "api", "cli", "json", "jsonl", "css", "scss",
    "html", "http", "https", "url", "uri", "urls", "id", "ids", "uuid", "sha",
    "md", "py", "js", "ts", "jsx", "tsx", "csv", "tsv", "yaml", "yml", "toml",
    "ini", "xml", "svg", "png", "jpg", "jpeg", "gif", "pdf", "regex", "regexp",
    "stderr", "stdout", "stdin", "ascii", "utf", "ssl", "tls", "ssh", "tcp",
    "udp", "ip", "dns", "cdn", "gpu", "cpu", "ram", "os",
    # state / status / process
    "ok", "up", "pm", "am", "ci", "cd", "db", "ui", "ux", "vm", "pid", "env",
    "repo", "repos", "async", "await", "kwargs", "args", "argv", "venv",
    "config", "configs", "lru", "ttl", "fifo", "lifo", "wip", "todo", "todos",
    "fixme", "git", "vcs", "diff", "sed", "awk", "grep", "lint", "linter",
    # cloud / infra
    "aws", "gcp", "k8s", "kube", "kubectl", "docker", "nginx", "redis",
    "postgres", "mysql", "sqlite", "mongo", "fastapi", "uvicorn", "gunicorn",
    "asgi", "wsgi", "crud", "jwt", "rbac", "iam", "vpc",
    # python / js ecosystem tokens read fine aloud
    "py", "ipynb", "wheel", "whl", "pyc", "pydantic", "numpy", "pandas",
    "pytorch", "tensorflow", "kokoro", "ollama", "tts", "llm", "ml", "ai",
    # misc dev shorthand the dict lacks
    "macos", "linux", "ubuntu", "iso", "rfc", "url", "localhost", "healthz",
    "redoc", "openapi", "lgpl", "gpl", "mit", "bsd", "apache", "lofi",
    # short dev shorthand the dict lacks even after stemming
    "deps", "dep", "infra", "init", "auth", "fmt", "impl",
})

# A version/unit token: 4k, 8gb, 476s, 7m, v1, v2, 3.5, 60fps, 12.3s.
# Number with an optional short unit suffix, OR a vN version, OR a dotted num.
_NUM_UNIT_RE = re.compile(
    r"""^(?:
        v?\d+(?:\.\d+)*           # 1 / 1.2.3 / v1 / v2
        | \d+(?:\.\d+)?[a-z]{1,4} # 4k / 8gb / 476s / 7m / 60fps / 12.3s
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# A word token candidate (the only thing we run the dict test on). 2+ letters,
# allowing internal apostrophe / hyphen ("don't", "build-id").
_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{1,}")

_VOWELS = frozenset("aeiouyAEIOUY")


def _load_bundled_dict() -> frozenset[str]:
    """Load the bundled public-domain word list (daemon/data/words.txt.gz) into a
    lowercased frozenset. Bundled so the dictionary gate behaves identically on
    every platform: Linux/CI hosts ship no /usr/share/dict/words, which would
    silently disable the zero-real-word noise drop (gibberish then spoken). See
    daemon/data/NOTICE.md for provenance."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "words.txt.gz")
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as fh:
            return frozenset(w.strip().lower() for w in fh if w.strip())
    except OSError:
        return frozenset()


def _load_system_dict() -> frozenset[str]:
    """Load a word list ONCE into a lowercased frozenset: the OS list
    (/usr/share/dict/words) if present, else the bundled public-domain copy so
    the dictionary gate works identically on every platform. Returns EMPTY only
    if even the bundle is unreadable — callers treat an empty dict as 'skip the
    dictionary part of the gate' so the rest of is_speakable still works."""
    for path in ("/usr/share/dict/words", "/usr/dict/words"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return frozenset(w.strip().lower() for w in fh if w.strip())
        except OSError:
            continue
    return _load_bundled_dict()


_SYSTEM_DICT = _load_system_dict()


def _in_dict_or_stem(low: str) -> bool:
    """True if `low` is a dict/allowlist word OR a regular inflection of one.

    /usr/share/dict/words is a BASE-FORM list (no '-ed'/'-ing'/'-s' forms), so
    "passed", "failed", "running", "insertions", "imports" are all absent even
    though their stems exist. A small inflectional stemmer recovers them without
    a hand-maintained word list — this is what keeps "23 passed, 4 failed" (the
    #1 dev status line) speakable. Conservative: only well-known English
    suffixes, and every candidate stem must itself be a real dict/allowlist
    word, so we never invent vocabulary."""
    if low in _SYSTEM_DICT or low in _TECH_ALLOWLIST:
        return True

    def known(stem: str) -> bool:
        return stem in _SYSTEM_DICT or stem in _TECH_ALLOWLIST

    # plural / 3rd-person -s, -es, -ies
    if low.endswith("ies") and known(low[:-3] + "y"):       # deletions? no — "ies"->"y"
        return True
    if low.endswith("es") and len(low) > 3 and known(low[:-2]):   # boxes->box
        return True
    if low.endswith("s") and len(low) > 2 and known(low[:-1]):    # imports->import
        return True
    # past / progressive: -ed, -d, -ing  (+ doubled consonant + silent-e)
    for suf in ("ed", "ing"):
        if low.endswith(suf) and len(low) > len(suf) + 1:
            stem = low[: -len(suf)]
            if known(stem):                 # passed->pass, failing->fail
                return True
            if known(stem + "e"):           # used->use, making->make
                return True
            # doubled final consonant: stopped->stop, running->run
            if len(stem) >= 2 and stem[-1] == stem[-2] and known(stem[:-1]):
                return True
    if low.endswith("ied") and known(low[:-3] + "y"):       # tried->try
        return True
    # -ion / -ions noun forms: insertions->insert, deletions->delete
    if low.endswith("ions") and known(low[:-4]):
        return True
    if low.endswith("ion") and known(low[:-3]):
        return True
    if low.endswith("ions") and known(low[:-4] + "e"):      # insertions->inserte? no
        return True
    if low.endswith("tions") and known(low[:-5] + "te"):    # deletions->delete
        return True
    return False


def _is_real_word(tok: str) -> bool:
    """A word token is REAL if it's in the system dict, in the tech allowlist,
    a regular inflection of one, or a hyphenated compound of real parts.
    Conservative: when the dict is unavailable we fall back to the allowlist
    only (so we never DROP a legit word merely because the host lacks a word
    list — the empty-dict guard in is_speakable keeps the dict branch from
    firing in that case)."""
    low = tok.lower()
    if low in _TECH_ALLOWLIST:
        return True
    if not _SYSTEM_DICT:
        return False
    # Strip a trailing possessive 's ("daemon's") before stemming.
    base = low[:-2] if low.endswith("'s") else low
    if _in_dict_or_stem(base):
        return True
    # Hyphenated compound: real if every part is real ("build-ok", "drop-in").
    # A trailing/leading empty part (orphan hyphen like "agent-") does NOT count.
    if "-" in low:
        parts = low.split("-")
        if all(parts) and all(_in_dict_or_stem(p) for p in parts):
            return True
    return False


def _is_vowelless_token(tok: str) -> bool:
    """A 3+ char ALPHA token with no vowel — a strong gibberish tell
    (rw, drwxr, xr, user-style ls/ps residue). 1-2 char tokens are
    excluded (too many legit 2-char acronyms) and the acronym allowlist is
    checked by the caller before this is allowed to trip a drop."""
    if len(tok) < 3 or not tok.isalpha():
        return False
    return not any(c in _VOWELS for c in tok)


# A Unix permission string: optional type char (-/d/l/c/b/p/s) + 9 rwx/-
# bits, optionally with a trailing ACL '@'/'+', as `ls -l` emits.
# "-rw-r--r--", "drwxr-xr-x", "-r-xr--r--@". Reading these aloud is pure noise.
_PERM_BITS_RE = re.compile(r"^[-dlcbps][-rwxsStT]{9}[@+]?$")


def _has_permission_bits(text: str) -> bool:
    """True if a whitespace-delimited token looks like an `ls -l` permission
    string. The single most reliable signature that a line is a directory
    listing (which a screenless listener cannot use)."""
    for tok in text.split():
        if _PERM_BITS_RE.match(tok):
            return True
    return False


# An ALL_CAPS_SNAKE token immediately followed by '=' — the signature of an
# env-var assignment dump ("PLAN_START_TIME=... PLAN_START_EPOCH=..."), which a
# voice reads as digit-by-digit gibberish. Requires the underscore so a single
# bare assignment "A = B" (no snake token, spaces around '=') never matches; the
# token must be 2+ chars of UPPER/digit/_ with at least one letter, glued to '='.
_ENV_ASSIGN_RE = re.compile(r"(?<![\w=])[A-Z][A-Z0-9_]*_[A-Z0-9_]*=(?!=)")
# A numeric-ish data token: an int, float, or percentage (ps/top column value).
# Used to detect a RUN of consecutive numeric columns (a data dump) regardless
# of any leading word header that would otherwise inflate the real-word ratio.
_NUMERICISH_RE = re.compile(r"^[+\-]?\d[\d.,]*%?$")


def _has_env_assign_dump(text: str) -> bool:
    """True if the line is dominated by ALL_CAPS_SNAKE '=' assignments — an
    env-var/timestamp dump ("PLAN_START_TIME=... PLAN_START_EPOCH=..."). Requires
    >=2 such assignments OR a single one that IS essentially the whole line, so a
    lone "A = B" (no snake token) and ordinary prose mentioning one capitalized
    word are never tripped."""
    hits = _ENV_ASSIGN_RE.findall(text)
    if len(hits) >= 2:
        return True
    if len(hits) == 1:
        # A single ALL_CAPS_SNAKE= that dominates a short line (<=3 tokens) is
        # still an assignment dump ("FOO_BAR=123"), not prose.
        return len(text.split()) <= 3
    return False


def _has_numeric_dump(text: str) -> bool:
    """True if the line contains a RUN of >=4 consecutive whitespace-delimited
    numeric-ish tokens (ints/floats/pcts) — a ps/top/data-dump column row, even
    when a word header precedes the numbers ("=== Daemons running === user
    39608 13.8 0.1 442454016 48976 ..."). Guarded so "23 passed, 4 failed." (the
    numbers are separated by WORDS, never >=4 in a row) and a single long id
    (one token, not a run) both survive."""
    run = 0
    for tok in text.split():
        if _NUMERICISH_RE.match(tok):
            run += 1
            if run >= 4:
                return True
        else:
            run = 0
    return False


def is_speakable(text: str) -> bool:
    """True if `text` (already normalized) carries enough real words to be worth
    speaking — the precision backstop for residual noise the normalizer empties
    or thins out.

    Two gates, in order:

    Structural (original):
      * empty / whitespace-only text (e.g. a bare SHA the normalizer removed),
      * text with NO alphabetic word of length >= 3,
      * number/symbol-dominated dumps (ps output, env blobs) where letters are
        < 35% of the non-space characters.

    Semantic (dictionary-word-ratio, added R5):
      * a multi-token line whose word tokens are NONE real vocab (ls/ps output,
        agent-id dumps) — dropped,
      * a line carrying a multi-char VOWELLESS non-acronym token (drwxr, user
        column residue) when real words are a minority — dropped.

    Both gates are conservative: a SINGLE unknown token inside otherwise-real
    prose is always KEPT. Keeps real status a screenless listener wants:
    "Build OK", "3 errors", "23 passed, 4 failed", "Duration 7m (476s)".
    """
    if not text or not text.strip():
        return False
    if not _SPEAKABLE_WORD_RE.search(text):
        return False
    letters = sum(c.isalpha() for c in text)
    non_space = sum(1 for c in text if not c.isspace())
    if non_space and letters / non_space < 0.35:
        return False

    # --- semantic gate ----------------------------------------------------
    word_tokens = _WORD_TOKEN_RE.findall(text)
    # Need >=2 word tokens before the dictionary verdict can fire: a single
    # unknown token ("UP", a product name) must always pass (precision).
    # (c) A line that BEGINS with an `ls -l` permission string is a directory
    #     listing the listener cannot use ("-rw-r--r-- 1 user wheel …",
    #     "drwxr-xr-x …"). Anchored to the FIRST token so a line that merely
    #     mentions perm bits AFTER real prose ("uv 0.8.9 … -rw-r--r-- …") is
    #     kept — the leading content is the spoken value.
    first_tok = text.split(maxsplit=1)[0] if text.split() else ""
    if _PERM_BITS_RE.match(first_tok):
        return False

    # (d) Env-var assignment dump — a line dominated by ALL_CAPS_SNAKE '='
    #     tokens ("PLAN_START_TIME=... PLAN_START_EPOCH=...") is a timestamp/
    #     config dump a voice reads digit-by-digit. Dict-independent so it fires
    #     even on a host without a word list. Guarded against "A = B" / prose.
    if _has_env_assign_dump(text):
        return False
    # (e) Numeric column dump — a RUN of >=4 consecutive numeric-ish tokens is a
    #     ps/top/data row ("... 39608 13.8 0.1 442454016 48976 ..."), regardless
    #     of any leading word header that would inflate the real-word ratio.
    #     "23 passed, 4 failed." (numbers split by words) and a single long id
    #     (one token, not a run) both survive.
    if _has_numeric_dump(text):
        return False

    # When the host has NO word list, _is_real_word only knows the allowlist,
    # so a ZERO-real verdict would be meaningless (it would drop "race condition
    # found"). Gate the dictionary-ratio branches on having a real dict.
    if _SYSTEM_DICT and len(word_tokens) >= 2:
        real = sum(1 for t in word_tokens if _is_real_word(t))
        ratio = real / len(word_tokens)
        # (a) ZERO real words across 2+ tokens → pure non-vocab gibberish
        #     (agent-id dumps "agent-ab67… agent-ac00…", orphan-hyphen runs).
        if real == 0:
            return False
        # (b) A multi-char vowelless non-acronym token present AND real words
        #     are a minority (< ~0.34) → ps/permission-bit residue dominated by
        #     vowelless column junk.
        if ratio < 0.34:
            for t in word_tokens:
                if t.lower() in _TECH_ALLOWLIST:
                    continue
                if _is_vowelless_token(t):
                    return False
    return True


# Curated code/file extensions whose leading '.' must be spoken as " dot " so the
# TTS engine does not read it as a sentence terminator and pause mid-utterance
# (reported on "claude-tts.tsx"). A curated allowlist keeps prose safe: "e.g.",
# "U.S.A", and decimals like "3.5" never match.
_SPEAKABLE_EXTS = frozenset({
    "py", "pyi", "ipynb", "js", "mjs", "cjs", "jsx", "ts", "tsx", "json", "jsonc",
    "md", "mdx", "txt", "rst", "rs", "go", "rb", "java", "kt", "kts", "swift",
    "c", "h", "cc", "cpp", "cxx", "hpp", "cs", "php", "pl", "lua", "jl", "dart",
    "ex", "exs", "sh", "bash", "zsh", "fish", "ps1", "bat", "sql", "html", "htm",
    "css", "scss", "sass", "less", "vue", "svelte", "astro", "yml", "yaml", "toml",
    "ini", "cfg", "conf", "env", "lock", "xml", "csv", "tsv", "log", "gitignore",
    "dockerfile", "makefile", "gradle", "proto", "graphql",
})
# basename.ext — extension must START with a letter (so decimals "3.5" never
# match) and be reasonably short. The basename allows letters/digits/_/- (so
# "claude-tts.tsx" is one unit).
_FILE_EXT_RE = re.compile(r"\b([A-Za-z0-9_-]+)\.([A-Za-z][A-Za-z0-9]{0,9})\b")


def speak_file_extensions(text: str) -> str:
    """Speak "name.ext" as "name dot ext" for known file extensions.

    The bare '.' between a basename and its extension is otherwise read by the TTS
    engine as a sentence terminator, inserting an unnatural pause mid-sentence
    (e.g. "claude-tts.tsx"). Only a curated set of real code/file extensions is
    rewritten, so a sentence end ("Done. Next."), a decimal ("3.5"), and prose
    abbreviations ("e.g.") are left alone. Idempotent: the output contains no
    "name.ext" pair to re-match.
    """
    if not text or "." not in text:
        return text

    def _repl(m: "re.Match[str]") -> str:
        if m.group(2).lower() in _SPEAKABLE_EXTS:
            return f"{m.group(1)} dot {m.group(2)}"
        return m.group(0)

    return _FILE_EXT_RE.sub(_repl, text)


# Number + unit -> spoken words, and ~/≈ before a number -> "about". Runs after
# the markup fixed point (units survive stripping) so "~1.1s" reads "about 1.1
# seconds" instead of the engine voicing the bare "~" and "s". Digits are left
# as-is (the engine reads "1.1" as "one point one"). Idempotent: the expanded
# form has a SPACE before the word, so the no-space <num><unit> pattern can't
# re-match.
_APPROX_RE = re.compile(r"[~≈]\s*(?=\.?\d)")
# Unit must ABUT the number and NOT follow a word-char or '=' — so prose,
# identifiers (test_5s), key=value (timeout=5s), and the interior digits of
# compound tokens (3m24s, 10s20s) are never touched. "ms" before "s" wins the
# alternation. "%" additionally must not abut a FOLLOWING letter, else it glues
# into an unspeakable word ("100%CPU" -> "percentCPU").
_NUM_TIME_RE = re.compile(r"(?<![\w=])([0-9]+(?:\.[0-9]+)?)(ms|s)\b")
# "%+" consumes a run so "100%%" (printf escape) -> "100 percent", not
# "100 percent%"; the trailing lookahead also blocks a following digit or "%"
# so we never glue onto a letter ("100%CPU") or orphan a second "%". ASCII
# digits only ([0-9], not \d) so fullwidth/Arabic-Indic numerals aren't half-expanded.
_NUM_PCT_RE = re.compile(r"(?<![\w=])([0-9]+(?:\.[0-9]+)?)%+(?![A-Za-z0-9%])")
_TIME_UNIT_WORDS = {"ms": "milliseconds", "s": "seconds"}
# A bare integer + "s" that is a DECADE or informal PLURAL, not a duration:
# era context before a 2-digit number ("the 90s", "'90s"). 4-digit year-decades
# ("2020s") and "<n>s of" plurals ("100s of") are handled inline below.
_DECADE_PRE_RE = re.compile(r"(?:'|\b(?:the|early|late|mid)\s)$", re.I)


def speak_numeric_units(text: str) -> str:
    """Expand approximation marks and number+unit abbreviations for natural
    speech: "~1.1s" -> "about 1.1 seconds", "150ms" -> "150 milliseconds",
    "24.0%" -> "24.0 percent", "1s" -> "1 second". Pure + idempotent.

    Only a unit ABUTTING a number is expanded (s, ms, %), and identifiers
    (test_5s), key=value (timeout=5s), compound tokens (3m24s), versions
    (v0.1.6), IPs, and letter-glued percents (100%CPU) are all left untouched.
    The bare-integer seconds path also skips decades/plurals: 4-digit years
    ("2020s"), "<n>s of" ("100s of errors"), and era-prefixed 2-digit decades
    ("the 90s", "'90s"). Residual: a bare 2-digit "90s" with no era word before
    it still reads as seconds (genuinely ambiguous). "m"/"h" are left alone
    (minutes vs metres vs millions).
    """
    if not text:
        return text or ""
    text = _APPROX_RE.sub("about ", text)

    def _time(m):
        num, unit = m.group(1), m.group(2)
        if unit == "s" and "." not in num:        # bare integer seconds: guard
            if len(num) >= 4:                      # "2020s" year/decade, not a duration
                return m.group(0)
            if m.string[m.end():m.end() + 3].startswith(" of"):  # "100s of" plural
                return m.group(0)
            if len(num) == 2 and _DECADE_PRE_RE.search(m.string[:m.start()]):
                return m.group(0)                  # "the 90s" / "'90s" decade
        word = _TIME_UNIT_WORDS[unit]
        if num == "1":
            word = word[:-1]                       # singular: "1 second", "1 millisecond"
        return f"{num} {word}"

    text = _NUM_TIME_RE.sub(_time, text)
    text = _NUM_PCT_RE.sub(r"\1 percent", text)
    return text


def normalize_for_speech(text: str) -> str:
    """Destructure markdown/markup to clean speech. Pure, idempotent, safe.

    Iterates _normalize_once() to a FIXED POINT, so idempotence holds by
    construction: an internal step (e.g. box-char removal re-exposing a rule or
    table-separator line that is only classified at the top of a pass) can need
    a second pass, and normalize() returns a value it maps to itself.
    """
    if not text:
        return text or ""
    result = _normalize_once(text)
    for _ in range(4):  # converges in 1-2 in practice; bounded for safety
        nxt = _normalize_once(result)
        if nxt == result:
            break
        result = nxt
    # Filenames: speak "name.ext" as "name dot ext" so the engine doesn't read the
    # extension dot as a sentence end (an unnatural mid-sentence pause). Applied
    # after the markup fixed point and before any path humanization; idempotent
    # (the output contains no "name.ext" pair to re-match).
    result = speak_file_extensions(result)
    # NOTE: number/unit expansion (speak_numeric_units) is deliberately NOT done
    # here — it's a final render step applied by ProcessStage._clean_text_sync
    # AFTER the is_speakable gate, so expanding "5s"/"40%" into words can't push
    # a number-dump past the speakability filter.
    return result


def _normalize_once(text: str) -> str:
    """One markup->speech pass. Not guaranteed idempotent alone; the public
    normalize_for_speech() wraps this in a fixed-point loop."""
    if not text:
        return text or ""

    # HTML entities -> characters; loop so double-escaped entities resolve
    # fully (&amp;amp; -> &amp; -> &) — required for idempotence.
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    # Non-breaking / exotic unicode spaces -> normal space, so normalize is
    # self-consistent regardless of caller (not reliant on a later split()).
    text = text.replace("\xa0", " ")
    # Fenced code blocks (and any unbalanced remnant) are unspeakable.
    text = _FENCE_RE.sub(" ", text)
    text = _FENCE_OPEN_RE.sub(" ", text)

    out: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line
        if not line.strip():
            out.append("")
            continue
        # Whole-line rules / table separators vanish.
        if _RULE_LINE_RE.match(line) or _TABLE_SEP_RE.match(line):
            continue
        # A genuine table ROW (BOTH leads and trails with '|', >=2 pipes) gets
        # its cells joined with commas. A shell pipe ('a | b', or a trailing
        # 'a | b |') is NOT a table and is left untouched.
        stripped = line.strip()
        if line.count("|") >= 2 and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            line = ", ".join(c for c in cells if c)
        # Loop prefix + inline stripping to a FIXED POINT: removing a leading
        # box-char/decoration can re-expose a line-leading marker (>, -, #) that
        # a single pass would leave for the NEXT normalize() call. Looping makes
        # one call idempotent.
        prev = None
        while prev != line:
            prev = line
            line = _strip_line_prefixes(line)
            line = _strip_inline_markup(line)
        if not line.strip():
            continue
        out.append(line)

    text = "\n".join(out)
    text = re.sub(r"[ \t]{2,}", " ", text)   # collapse internal runs
    text = re.sub(r"[ \t]+\n", "\n", text)   # trailing space per line
    text = re.sub(r"\n{3,}", "\n\n", text)   # cap blank-line runs
    return text.strip()
