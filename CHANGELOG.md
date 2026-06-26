# Changelog

All notable changes to claude-tts are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.5] — 2026-06-25

### Added
- Sub-agent following is back, now **cwd-scoped** (the safe version of the
  feature deferred in v0.1.4). Each spoken-log entry records the project dir
  (cwd) it was spoken in; the daemon learns a session's cwd from its hook events
  (`spoken_log.note_session_cwd`) and `append` stamps it. `read_merged()` and the
  `/tts:log` merge now only fold in sibling-agent lines whose cwd matches this
  session's, and the statusline `subagent_aware` pivot does the same. Sub-agents
  and background agents inherit their parent's cwd, so they surface; an unrelated
  concurrent session in another directory never does — which is what made the old
  "follow the newest file" pivot mirror two sessions together. Entries that
  predate the cwd field fall back to the old time-only behavior. `read_merged()`
  also accepts an explicit `cwd=` so a caller (e.g. `/tts:log`) can pass its own
  `os.getcwd()`. +3 tests in `tests/test_spoken_log.py`.

### Changed
- `statusline.subagent_aware` / `active_window_s` / `include_subagent_in_main`
  are no longer "inert/deferred" (v0.1.4) — they are active and cwd-scoped. Docs
  (README, `docs/CONFIGURATION.md`) updated accordingly.

## [0.1.4] — 2026-06-25

### Fixed
- The summarizer no longer speaks its own prompt aloud. The tiny summarizer
  model (`qwen2.5-coder:1.5b`) would occasionally echo the SUMMARIZE rule block
  instead of summarizing ("No 'Here's', no preamble. First person, never 'we'…"),
  and that echo was spoken verbatim. `_call_ollama` now runs an instruction-echo
  detector (`_looks_like_prompt_echo`, ≥2 instruction-only signatures) on the
  model output and falls back to the deterministic rule-based summary on a hit.
  Pure string logic, gated by `tests/test_summarizer_echo.py`.
- Statusline spoken-log is now strictly session-scoped. The previous behavior
  picked the newest spoken-log file in the whole directory to "follow the most
  recently active agent", so two Claude Code sessions running from different
  directories **mirrored each other's spoken output** on the statusline. The
  pivot is removed; the statusline now shows only the current session's own log.
- Harness boilerplate is no longer spoken or repeated. Claude Code appends a
  `Shell cwd was reset to <path>` line to a Bash result when the command ran from
  a different cwd; it rode in on the varying *tail* of stdout, so whole-content
  dedupe missed it and it was spoken several times in a row. It's now stripped at
  the single stdout/stderr chokepoint before classification. A general
  **consecutive-tail guard** also caps utterances that share an identical last
  line at 2 in a row (a different line in between resets it), catching any
  stable-trailing-line noise that whole-content dedupe can't.

### Changed
- Sub-agent *following* on the statusline is deferred until spoken-log entries
  record a cwd/project tag (without it, "follow the newest agent" is
  indistinguishable from "follow an unrelated concurrent session"). The
  `statusline.subagent_aware` / `active_window_s` keys are kept as
  forward-compatible placeholders but are **currently inert**;
  `include_subagent_in_main` still merges by time-overlap across *all* sessions
  and should stay `false` when running multiple projects at once. Docs (README,
  `docs/CONFIGURATION.md`) updated to describe the shipped session-scoped
  behavior accurately.

## [0.1.3] — 2026-06-25

### Fixed
- Turn summaries are no longer filtered as tool-output noise. The assistant's
  end-of-turn summaries were run through the same regex ladder tuned for raw
  command stdout, so ordinary markdown in prose (a `-----` rule, "N files
  changed", a commit SHA) false-positived and silenced the whole summary. The
  drop filter now has a `prose` mode (stop_events) that skips those mid-content
  patterns while still dropping bare code blocks / file paths, duplicates, and
  system reminders. Tool-output filtering is unchanged.

### Added
- Cursor-editor TTS integration: hooks (`cursor-pre-tool-use.sh`,
  `cursor-post-tool-use.sh`, `cursor-after-agent-response.sh`) + a normalizer
  (`cursor_normalize.py`) that map Cursor's hook events to the daemon, plus a
  `CLAUDE_TTS_PASSTHROUGH` stdout gate in the Claude Code hooks (default on →
  unchanged behavior). Note: the after-response hook uses GNU `timeout`
  (preinstalled on Linux; on macOS install coreutils, e.g. `brew install
  coreutils`) for its daemon dispatch.

## [0.1.2] — 2026-06-25

### Added
- Sub-agent / background-agent-aware spoken log. Each sub-agent and background
  agent already gets its own `session_id` from Claude Code, so the per-session
  spoken-log file already separates them. New `spoken_log.read_merged()` folds
  time-overlapping sibling-agent lines into one view, and two view-only config
  flags in the new `statusline` block control it: `subagent_aware` (the
  statusline segment follows whichever agent spoke most recently) and
  `include_subagent_in_main` (`/tts:log` merges sub-agent lines into the main
  view). No daemon changes — purely a display choice.
- Disk guard: `GenerateStage` now checks free space before each synthesis write
  (`min_free_bytes`, 200 MB default). When the cache volume is nearly full it
  evicts aggressively and, if still low, refuses to synthesize and fires a loud
  alert (desktop notification + statusline warning) instead of silently muting
  — fixing the recurring disk-full mute where every engine appears to fail.

### Changed
- CI now runs an informational (non-blocking) async test suite so async pipeline
  regressions are visible, not just the sync `make verify` gate.

## [0.1.1] — 2026-06-25

### Added
- Spoken-output log: the daemon now records every utterance it actually speaks
  to a bounded, per-session JSONL at `~/.claude/logs/tts/spoken/<session>.jsonl`
  (`daemon/spoken_log.py`, best-effort — a logging failure never breaks
  playback), surfaced by the new `/tts:log` command (newest-first, with
  timestamps and category).
- README demo: an animated terminal GIF, a matching audio sample, and a
  combined MP4 (picture + sound), generated by replaying
  `tests/fixtures/event_corpus.jsonl` through the real `ContentRouter`
  (`scripts/demo_gif.py`, `scripts/demo_audio.py`) — the demo is derived from
  the same corpus that gates CI, so it can't drift from behavior.
- `docs/ARCHITECTURE.md` — the filter brain, the three seams, the async
  pipeline, backpressure tiers, and the markdown→speech chokepoint.
- `docs/CONFIGURATION.md` — every config block, code-verified defaults, the
  timeout-clamp invariant, and the full environment-variable table.

### Fixed
- Long-utterance playback cutoff: a long spoken answer (e.g. a commit message
  read at the Stop hook) is no longer truncated mid-read when the next event
  escalates the session to the BLACK drift tier. `flush_buffer` now preserves
  the in-progress utterance's own tail chunks, matched by a persisted
  `PlaybackState.current_request_id`, instead of discarding them as backlog.
- Git SHA-range leak: a revision range `sha1..sha2` now drops BOTH hashes — the
  second was shielded by the dotted-identifier lookbehind and leaked to speech.
- Filename cadence: `name.ext` for a known code/file extension is spoken as
  "name dot ext", so the engine no longer reads the extension dot as a sentence
  end and pauses mid-utterance (reported on "claude-tts.tsx"). Prose, decimals
  ("3.5"), and abbreviations ("e.g.") are left untouched.
- Documentation drift: the README no longer describes `/tts:setup` as a future
  milestone, and the setup skill no longer claims Linux service install is
  unautomated — both shipped in 0.1.0.

## [0.1.0] — 2026-06-25

First packaged release: a local text-to-speech daemon for Claude Code, with
smart content routing, hooks, and graceful degradation.

### Added
- Three swappable seams: LLM provider (`ollama` / OpenAI-compatible / `null`),
  TTS engine (`kokoro` / `voicebox` / `edge-tts` / `say` / `espeak`), and OS
  platform (audio playback + service install).
- Zero-dependency fallback engine: macOS `say` / Linux `espeak`, so TTS works
  with no ML or network dependencies.
- Cross-platform audio playback: macOS `afplay`; Linux auto-detects
  `ffplay`/`mpv`/`pw-play`/`paplay`/`aplay` (decoders first, for format safety).
- Background service install: macOS `launchd`, Linux `systemd --user`
  (`enable --now` + `loginctl enable-linger`). Windows points to WSL2/Docker.
- Commands: `/tts:setup` (mini-eval calibration + service install + config),
  `/tts:doctor` (idempotent health checks), `/tts:uninstall`.
- Binding quality gate (`make verify`) — deterministic, no live deps.
- CI matrix (GitHub Actions): macOS + Linux × Python 3.11–3.13.
- Pinned dependencies (`uv.lock`) and version-synced manifests
  (`make manifests`).

### Notes
- Volume control is macOS-only (`afplay -v`); on Linux the audio daemon owns
  system volume.
- Windows service install and Linux/Windows volume are not yet implemented.
