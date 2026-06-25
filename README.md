# claude-tts

Hear your coding agent. **claude-tts** speaks a *curated, filtered* stream of a
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) agent's work — the
status pivots, the errors, the final answers — and stays quiet through the noise.
A local LLM judges what's worth saying and summarizes the long bits; a TTS engine
synthesizes it; Claude Code hooks drive the whole thing. Local-first and
token-free by default.

> **Status:** this repository is the sanitized core (daemon + filter brain +
> test gate). The one-command Claude Code plugin installer (`/tts:setup`) is the
> next milestone; for now see [Manual setup](#manual-setup).

## How it works

```
Claude Code hooks ──▶ unix socket ──▶ daemon
                                        │
        ContentRouter (classify · judge · summarize)   ← the filter brain
                                        │
        QueueManager ▶ Orchestrator ▶ Generate ▶ Playback
                          (LLM provider seam)  (TTS engine)  (OS audio)
```

The filter brain is the value: it decides **what** to surface and **how** to
phrase it. Synthesis and playback are swappable behind seams.

- **LLM provider seam** (`daemon/providers/`) — `judge(text) → speak?` and
  `summarize(text) → str`. Ships `ollama` (local default), `openai_compat`
  (any OpenAI-compatible `base_url`: LM Studio, llama.cpp, vLLM, Groq, …), and
  `null` (a deterministic, no-LLM floor — see below).
- **TTS engine** (`daemon/pipeline/`) — `kokoro` (local MLX), `voicebox`
  (local app via REST), and `edge-tts` (cross-platform Azure voices).
- **Playback** — macOS `afplay`; Linux auto-detects `ffplay`/`mpv`/`pw-play`/`paplay`/`aplay` (decoders first); Windows `ffplay`.

### No-LLM fallback

With `llm_provider.type = "null"`, the system still works using deterministic
rules: it speaks structured signals (test counts, errors, status) and drops
noise, summarizing by truncation. The LLM is an *intelligence upgrade*, not a
hard dependency.

## Requirements

- **Python ≥ 3.11** and [`uv`](https://docs.astral.sh/uv/).
- **macOS** (`afplay` + `launchd`) or **Linux** (auto-detected audio player +
  `systemd --user`). On Windows, run the daemon manually or use WSL2/Docker.
- For the default **LLM provider**: a local [Ollama](https://ollama.com) with a
  small model, e.g. `ollama pull qwen2.5-coder:1.5b`. Or point at any
  OpenAI-compatible server. Or run with no LLM at all.
- For an **engine**: `edge-tts` (the `edge` extra, needs internet) or — on
  Apple Silicon — `kokoro` via a separate `mlx-audio` interpreter (see below).

## Manual setup

```bash
git clone https://github.com/chendrizzy/claude-tts
cd claude-tts
uv sync --extra edge        # base deps + the edge-tts engine
cp config.example.json config.json   # edit to taste
```

Wire the hooks in `hooks/` into your Claude Code settings (the registry is
`hooks/hooks.json`), then start the daemon (the `SessionStart` hook launches it
automatically). Verify it's alive: the daemon binds the socket in
`advanced.socket_path` and a test utterance plays.

### Engines

`edge-tts` needs no extra setup. The **kokoro** engine runs
`daemon/kokoro_worker.py` under an interpreter that has `mlx-audio` installed
(Apple-Silicon wheels) — set `MLX_PYTHON` (see `.env.example`) or
`voice.mlx_python` to that interpreter. **voicebox** offloads synthesis to the
local [Voicebox](https://voicebox.sh) app via its REST API.

## Configuration

Copy `config.example.json` and edit. Every block is optional (the daemon embeds
safe defaults). Key knobs: `voice.engine`, `llm_provider.type`,
`summarizer.model`, `filtering.max_response_length`.

## Development

`make verify` is the **binding quality gate** — a deterministic, all-sync suite
(no live daemon, Ollama, or `pytest-asyncio` needed) that fails on markdown
leaking to speech, classification regressions, and path-humanization bugs.

```bash
uv run make verify
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full test layout and how to add a
provider or engine.

## License

[MIT](LICENSE) © chendrizzy
