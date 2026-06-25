# claude-tts

[![tests](https://github.com/chendrizzy/claude-tts/actions/workflows/test.yml/badge.svg)](https://github.com/chendrizzy/claude-tts/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![local-first](https://img.shields.io/badge/local--first-token--free-success.svg)](#no-llm-fallback)

**Hear your coding agent.** claude-tts speaks a *curated, filtered* stream of a
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) agent's work — the
status pivots, the errors, the final answers — and stays quiet through the noise.
A local LLM judges what's worth saying and summarizes the long bits; a TTS engine
synthesizes it; Claude Code hooks drive the whole thing. Local-first and
token-free by default.

<p align="center">
  <img src="docs/media/demo.gif" alt="claude-tts deciding, in real time, what to speak from a Claude Code session: Read/Edit stay quiet; test results, an error, and the final answer are spoken aloud." width="100%">
</p>

> ▶ **[Watch the ~14s clip with sound](docs/media/demo.mp4)** · **[audio only](docs/media/sample.mp3)** — the exact lines marked SPEAK above, voiced by the default `edge-tts` engine.

The frame above isn't a mockup: it's `tests/fixtures/event_corpus.jsonl` replayed
through the real classifier (`scripts/demo_gif.py`). The same corpus gates CI, so
the demo can't drift from what the daemon actually does.

## What it says — and what it doesn't

The value isn't the voice, it's the *judgment*. Default verdict is **silence**;
only four kinds of event earn speech.

| A Claude Code event… | Verdict | …becomes |
|----------------------|---------|----------|
| `Read`/`Edit`/`Write` succeeds | 🔇 quiet | — |
| `git status`, a file listing, fenced code, a repeat | 🔇 quiet | — |
| `pytest` → `23 passed, 4 failed in 12.3s` | 🔊 **status** | *"In the tests: 23 passed, 4 failed."* |
| a command writes to stderr | 🔊 **error** *(pre-empts)* | *"cat: /nonexistent: No such file or directory"* |
| "★ Insight: the timeout fires before the handshake…" | 🔊 **insight** | *(spoken; long ones summarized)* |
| the assistant's end-of-turn answer | 🔊 **final answer** | *"Done. The bug was a missing await on the queue.put call."* |

And it cleans markup *before* it speaks, so you never hear punctuation read out:

| In the agent's raw output | Spoken |
|---------------------------|--------|
| `` Run `pytest -q` now `` | Run pytest -q now |
| `Fixed the **race** in` `queue_manager.py` | Fixed the race in queue_manager.py |
| `the value 2**8 equals 256` | the value 2\*\*8 equals 256 *(math kept, not "bold")* |
| `checked out agent-a1b2c3d4e5 worktree` | checked out worktree *(hash dropped)* |
| `## Summary` · `[the docs](https://…)` | Summary · the docs |

> Replaying 6,695 real spoken excerpts through the normalizer, markdown leaked
> into speech in **23.8%** of them before this cleaner and **0.0%** after — a
> figure asserted on every run by `make verify` (`tests/test_shadow_replay.py`).

## How it works

```
Claude Code hooks ──▶ unix socket ──▶ daemon
                                        │
        ContentRouter (classify · judge · summarize)   ← the filter brain
                                        │
        QueueManager ▶ Orchestrator ▶ Generate ▶ Playback
                          (LLM provider seam)  (TTS engine)  (OS audio)
```

The filter brain decides **what** to surface and **how** to phrase it. Synthesis
and playback are swappable behind three seams. For the full picture — the
classification ladder, backpressure tiers, error pre-emption, the markdown→speech
chokepoint — see **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

- **LLM provider seam** (`daemon/providers/`) — `judge(text) → speak?` and
  `summarize(text) → str`. Ships `ollama` (local default), `openai_compat`
  (any OpenAI-compatible `base_url`: LM Studio, llama.cpp, vLLM, Groq, …), and
  `null` (a deterministic, no-LLM floor — see below).
- **TTS engine seam** (`daemon/engines/`, `daemon/pipeline/`) — `edge-tts`
  (cross-platform Azure voices), `say`/`espeak` (zero-dependency system engine),
  `kokoro` (local MLX, Apple Silicon), and `voicebox` (local app via REST).
- **Platform seam** (`daemon/platforms/`) — macOS `afplay` + `launchd`; Linux
  auto-detected player + `systemd --user`; Windows `ffplay` (run daemon manually).

### No-LLM fallback

With `llm_provider.type = "null"`, the system still works on deterministic rules:
it speaks structured signals (test counts, errors, status) and drops noise,
summarizing by truncation. The LLM is an *intelligence upgrade*, not a hard
dependency.

## Install

claude-tts is a Claude Code plugin. Once it's added as a marketplace source:

```
/plugin marketplace add chendrizzy/claude-tts
/tts:setup
```

`/tts:setup` detects your platform, picks an engine and LLM backend, calibrates
the backend against a bundled mini-eval, installs the background service
(`launchd` / `systemd --user`), and writes your config. Re-runnable and
idempotent; `/tts:doctor` re-checks health anytime.

<details>
<summary><b>Manual setup</b> (development, or running without the plugin)</summary>

```bash
git clone https://github.com/chendrizzy/claude-tts
cd claude-tts
uv sync --extra edge          # base deps + the edge-tts engine
cp config.example.json ~/.config/claude-tts/config.json   # edit to taste
```

Wire the hooks in `hooks/` into your Claude Code settings (the registry is
`hooks/hooks.json`); the `SessionStart` hook launches the daemon automatically.
Verify it's alive: the daemon binds the socket (see
[Configuration](#configuration)) and a test utterance plays. The **kokoro**
engine additionally needs an `mlx-audio` interpreter pointed to by `$MLX_PYTHON`
(see [`.env.example`](.env.example)).
</details>

## Commands

| Command | What it does |
|---------|--------------|
| `/tts:setup` | First-run setup: engine, LLM backend, calibration, service, config |
| `/tts:status` | Daemon socket, active engine + model, recent log tail (read-only) |
| `/tts:doctor` | Disk, daemon/socket, deps, backend reachability — PASS/WARN + fixes |
| `/tts:voice` | Pick a speech engine and voice, then restart to apply |
| `/tts:uninstall` | Stop and remove the service/daemon (optionally the config) |

## Requirements

- **Python ≥ 3.11** and [`uv`](https://docs.astral.sh/uv/).
- **macOS** (`afplay` + `launchd`) or **Linux** (auto-detected audio player +
  `systemd --user`). On Windows, run the daemon manually or use WSL2/Docker.
- For the default **LLM provider**: a local [Ollama](https://ollama.com) with a
  small model, e.g. `ollama pull qwen2.5-coder:1.5b` — or any OpenAI-compatible
  server, or no LLM at all.
- For an **engine**: `edge-tts` (the `edge` extra, needs internet) or — on
  Apple Silicon — `kokoro` via a separate `mlx-audio` interpreter.

## Configuration

Copy [`config.example.json`](config.example.json) (every key is annotated inline)
and edit. Every block is optional; the daemon embeds safe defaults. Common knobs:
`voice.engine`, `llm_provider.type`, `summarizer.model`,
`filtering.max_response_length`. Full reference, defaults, and environment
variables: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

## Project layout

```
daemon/              the daemon — socket server, router, async pipeline, seams
  content_router.py    the filter brain: classify → judge → summarize
  pipeline/            ingest → process → generate → playback
  providers/           LLM seam: ollama · openai_compat · null
  engines/             TTS seam: edge-tts · system (say/espeak)
  platforms/           OS seam: launchd · systemd · audio players
hooks/               Claude Code hook scripts + hooks.json registry
commands/            slash commands (/tts:setup, status, doctor, voice, uninstall)
skills/tts-setup/    the first-run setup procedure
scripts/             calibration, manifest sync, the demo generators
tests/               the `make verify` gate + fixtures (spoken & event corpora)
docs/                ARCHITECTURE · CONFIGURATION · PUBLISH
```

## Development

`make verify` is the **binding quality gate** — a deterministic, all-sync suite
(no live daemon, Ollama, or `pytest-asyncio` needed) that fails on markdown
leaking to speech, classification regressions, and path-humanization bugs.

```bash
uv sync --extra edge --extra dev
uv run make verify
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test layout and how to add a
provider or engine, and [docs/PUBLISH.md](docs/PUBLISH.md) for the release
process. The demo media is regenerated with
`uv run --with pillow python scripts/demo_gif.py` (and `demo_audio.py`).

## License

[MIT](LICENSE) © chendrizzy
