# Architecture

claude-tts turns a Claude Code agent's work into a *filtered* spoken stream. The
hard part isn't synthesis — it's deciding **what** is worth saying and **how** to
phrase it so a status line doesn't become a paragraph of markdown read aloud.
That decision lives in one place (the `ContentRouter`); everything downstream is
swappable plumbing behind three seams.

```
 Claude Code                         the daemon (one per user, singleton-guarded)
 ───────────                         ─────────────────────────────────────────────
 PreToolUse  ┐                       ┌─ ContentRouter ── classify → (judge?) → (summarize?)
 PostToolUse ┼─▶ hook script ─▶ unix │     the filter brain · default = silence
 Stop        ┘   (JSON payload)  sock └─ QueueManager ── backpressure tiers · ERROR pre-empt
                                          │
                                          ▼  RoutedItem
                                   Ingest ▶ Process ▶ Generate ▶ Playback
                                            (clean       (TTS       (OS audio,
                                             markdown)    engine)     serial)
```

The flow: a Claude Code hook fires, a small shell script (`hooks/`) packages the
event as JSON and writes one line to the daemon's unix socket. The daemon
(`daemon/tts_daemon.py`) validates the payload against a schema, hands it to the
`ContentRouter`, and — if the router says speak — pushes it through an async
pipeline to your speakers. If the daemon isn't running, the hook fails silently:
no daemon, no noise, no broken builds.

## The filter brain — `daemon/content_router.py`

The router is a regex/heuristic ladder with one governing invariant: **the
default verdict is silence.** Anything that doesn't match a known-good shape is
dropped. It sorts each event into one of four speakable categories — or nothing:

| Category       | Priority | Examples                                            |
|----------------|----------|-----------------------------------------------------|
| `ERROR`        | 10       | non-empty stderr, tracebacks, `interrupted`, panics |
| `FINAL_ANSWER` | 7        | the assistant's end-of-turn message                 |
| `INSIGHT`      | 5        | "★ Insight", "the root cause is…", "turns out…"     |
| `STATUS`       | 5 / 3    | `23 passed, 4 failed`, build results, grep counts   |
| *(silence)*    | —        | `Read`/`Edit` success, file listings, dup content   |

Classification is mostly a sequence of cheap, ordered checks (`_classify_tool`,
`_classify_stop`):

1. **Schema sniff** — `tool_event` vs `stop_event`.
2. **Drop filter** (`_drop_check`, `_drop_check_raw`) — runs on *raw* stdout
   before any trimming, so multi-line shapes are visible: fenced code,
   `system-reminder` tags, `ls -la` listings, `grep -n` output, `git status`/
   `diff --stat` noise, symbol runs (`+++++`), and a recent-content dedupe window.
3. **Per-tool extractors** — Bash/Grep/Glob/Task/WebFetch each distill a signal
   (`23 passed, 4 failed.`, a match count, a subagent's last sentence, a page
   title) and name *what* produced it for the summarizer's context.
4. **The ambiguous middle** — substantive Bash output (long enough, contains a
   number *and* a domain keyword) that matched no extractor gets a single binary
   SPEAK/SKIP token from the LLM judge. This is the *only* place the LLM gates
   speech; everything above is deterministic.
5. **Phrasing** — content under 120 chars is spoken verbatim; longer content is
   marked for summarization.

Adding a new `Category` to the enum doesn't make it speak: categories must be on
an explicit `_SPEAKABLE_CATEGORIES` allowlist. New shapes are silent until
someone opts them in — fail-safe, not fail-loud.

## No-LLM floor — `daemon/providers/null_provider.py`

The LLM is an *intelligence upgrade, not a dependency.* With
`llm_provider.type = "null"`, `judge()` always returns `False` (so the ambiguous
middle stays silent) and `summarize()` falls back to `rule_based_summary()`.
The structured extractors still surface test counts, errors, and status, and
long content is shortened by truncation. You lose nuance, not function.

## The three seams

Each seam is a tiny abstract interface plus a factory that reads one config key.
Implementations are lazy-imported, so a missing optional dependency can never
break daemon startup.

### LLM provider — `daemon/providers/`
`judge(snippet, tool_name, context) → bool` and
`summarize(content, category, hint) → str`. Selected by `llm_provider.type`
(`daemon/providers/factory.py`):

- **`ollama`** (default) — local Ollama via HTTP; `judge` reuses the summarizer
  with a one-token prompt.
- **`openai`** — any OpenAI-compatible `base_url` (LM Studio, llama.cpp, vLLM,
  Groq, …); reads `base_url` / `model` / `api_key`.
- **`null`** — the deterministic floor above.

Every provider exposes `inner_timeout_s`. The router clamps its outer
`asyncio.wait_for` wrappers to *always* exceed it (`summarize_timeout_s` >
`inner_timeout_s`), because a wrapper that fires first would cancel the
summarize call and read the **raw markdown** instead. The clamp is in code, so a
mis-set config can't reintroduce that bug.

### TTS engine — `daemon/engines/` + `daemon/pipeline/`
`synthesize(text, out_path, voice, speed) → bool`. Selected by `voice.engine`
(`daemon/engines/factory.py`, with two engines that live in the pipeline):

- **`edge-tts`** (default) — Azure neural voices over the network, no local ML.
- **`say` / `espeak`** — the zero-dependency system engine (`SystemTTSEngine`);
  guaranteed audio on a bare machine.
- **`kokoro`** — local MLX Kokoro-82M via a *persistent worker subprocess*
  (`daemon/pipeline/kokoro_engine.py`) running under a separate `mlx-audio`
  interpreter (`$MLX_PYTHON`). Offline, Apple-Silicon.
- **`voicebox`** — offloads synthesis *and* playback to the local Voicebox app
  over REST (`daemon/pipeline/voicebox_client.py`).

### Platform — `daemon/platforms/`
`build_player_cmd()` / `spawn_player()` / `install_service()` /
`uninstall_service()`. Chosen by `platform.system()`:

- **macOS** — `afplay` (with `-v` volume); installs a `launchd` LaunchAgent.
- **Linux** — first available of `ffplay`/`mpv`/`pw-play`/`paplay`/`aplay`
  (decoders first, for format safety); installs a `systemd --user` unit and
  runs `loginctl enable-linger` so it survives logout.
- **Windows** — `ffplay`; service install is not implemented (run the daemon
  manually, or use WSL2/Docker).

## The async pipeline — `daemon/pipeline/`

`RoutedItem`s flow through four stages, coordinated by `orchestrator.py` and
bridged to the threaded daemon by `adapter.py`:

- **Ingest** (`ingest_stage.py`) — per-session queues woken by `asyncio.Event`
  (no polling latency).
- **Process** (`process_stage.py`) — the **single chokepoint** where markdown
  becomes speech: `daemon/text_utils.py::normalize_for_speech` strips fences,
  unwraps emphasis (while guarding `2**8`, `snake_case`, shell pipes), drops
  hashes/UUIDs/ANSI/ISO-timestamps, maps operators (`!=`→"not equal"), humanizes
  file paths, and restores contractions for natural prosody. Then it chunks for
  streaming synthesis. This pass is idempotent — running it twice changes
  nothing — which is what the `make verify` gate asserts.
- **Generate** (`generate_stage.py`) — cache-backed, per-session-parallel
  synthesis via the chosen engine; yields ordered `AudioSegment`s.
- **Playback** (`playback_stage.py`) — serial per session, with a cross-session
  lock so two Claude windows don't talk over each other, plus a watchdog that
  kills a hung player.

### Backpressure — `daemon/pipeline/queue_manager.py`

When the pipeline lags, the `QueueManager` raises a pressure tier and the router
gets stricter — borderline content stops being submitted rather than backing up
a stale audio queue:

| Tier   | Multiplier | Roughly when        | Effect                                   |
|--------|-----------|----------------------|------------------------------------------|
| GREEN  | 1.0       | lag < 3 s            | pass-through                             |
| YELLOW | 1.5       | 3–8 s                | coalesce same-category pairs             |
| RED    | 2.5       | 8–15 s               | drop low-priority status; force-summarize|
| BLACK  | 5.0       | > 15 s or queue > 50 | flush non-errors, say "still working"    |

Hysteresis keeps it from flapping (it only drops back to GREEN under ~2 s lag).
**`ERROR` is exempt from every gate** — it always speaks. An error queue-jumps
to the front of the playback buffer (`priority_enqueue`); it does *not* cut off
the currently-playing segment (that mid-utterance `SIGTERM` was removed because
slicing a sentence mid-word is worse than a half-second wait).

## Why you can trust the demo

The GIF and audio sample in the README aren't mockups — `scripts/demo_gif.py`
replays `tests/fixtures/event_corpus.jsonl` through the real `ContentRouter`
(no LLM) and renders whatever verdicts it returns. The same corpus gates CI, so
the demo can't drift from behavior. The "markdown leaked to speech: 23.8% →
0.0%" figure comes from `make verify`, which replays 6,695 real spoken excerpts
(`tests/test_shadow_replay.py`) against the normalizer.

See [CONFIGURATION.md](CONFIGURATION.md) for every knob, and
[CONTRIBUTING.md](../CONTRIBUTING.md) for how to add a provider or engine.
