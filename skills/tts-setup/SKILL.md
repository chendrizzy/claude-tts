---
name: tts-setup
description: Use for first-run setup of the claude-tts plugin — bootstraps the engine, LLM backend, OS service, and config on a fresh machine. Invoked by /tts:setup.
---

# claude-tts — First-Run Setup

First-run setup for the claude-tts plugin, invoked by `/tts:setup`. It is
**idempotent and re-runnable**; `/tts:doctor` re-checks health anytime.

**Fail-stop contract:** run the steps in order. After each step, verify it
succeeded. If a step cannot complete, report **which step failed** and the
**single most likely remediation**, then STOP — do not continue past a failed
step. Never hardcode absolute personal paths; the config dir is
`~/.config/claude-tts` and the socket is
`${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.

Let `ROOT` = the plugin/daemon root (`${CLAUDE_PLUGIN_ROOT}` when invoked as a
plugin). Run all Python through the project's uv venv: `uv run python ...`.

## Step 1 — Detect platform + architecture

Run `uname -s` (Darwin/Linux) and `uname -m` (arm64/x86_64). Record both. On
`Darwin`+`arm64` the machine is Apple Silicon (Kokoro is available). On Linux,
note that background-service install is not yet automated (Plan 4) — you will
run the daemon manually at Step 6.

## Step 2 — Ensure uv + environment

If `command -v uv` fails, install it: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
Then create the env and install deps from `ROOT`: `uv sync --extra edge` (this
project is a uv-managed application — its dependencies live in `pyproject.toml`
and the default `edge-tts` engine is the `edge` extra; there is no
`requirements.txt`). Verify `uv run python -c "import edge_tts"` succeeds.
Remediation on failure: ensure network access and that `~/.local/bin` is on PATH.

## Step 3 — Choose the speech engine

Default: `edge-tts` (cross-platform, no ML deps). If Apple Silicon, offer Kokoro
(`uv run python -c "import mlx_audio"` to check; the MLX model pulls on first use).
If a Voicebox app is reachable at its configured URL, offer `voicebox`. Record
the chosen `engine` and a default `voice_name` (`en-US-AvaNeural` for edge-tts).

If no ML engine is available and `uv sync --extra edge` cannot install edge-tts
(e.g. offline), choose `say` (macOS) / `espeak` (Linux) — the zero-dependency
**system engine**. It always produces audio on a bare machine (using the OS
default voice; `voice_name` is ignored for this engine).

## Step 4 — Choose the LLM backend

Default: local **Ollama** with model `qwen2.5-coder:1.5b`. Offer to pull it:
`ollama pull qwen2.5-coder:1.5b` (skip if `ollama list` already shows it).
Alternatively, an **OpenAI-compatible** endpoint (collect `base_url` + model;
read the API key from an environment variable the user names — never store the
raw key in shell history). Or **none** (deterministic mode). Record `backend`
∈ `ollama`/`openai`/`null`.

## Step 5 — Calibrate against the mini-eval

Run the calibration gate for the chosen backend:
`uv run python scripts/calibrate.py --backend <backend> [--model <m>] [--base-url <url>] [--api-key-env <ENVVAR>] --json`
It prints `{"mode": "smart"|"deterministic", ...}`. If `mode == "smart"`, keep
the chosen backend. If `mode == "deterministic"` (model unreachable or below the
precision/recall bar), warn the user that the LLM under-performed its own
deterministic floor and set `backend = null` for the config — TTS still works,
just rule-based. The `null` backend skips scoring and is deterministic by
definition.

## Step 6 — Install the OS service + start the daemon

Compute the launcher: `PYTHON = ROOT/.venv/bin/python`, `program_args =
[PYTHON, "-m", "daemon.tts_daemon"]`, `env = {"PYTHONUNBUFFERED": "1",
"PYTHONPATH": ROOT, "PATH": <current PATH>}`. (`PYTHONPATH=ROOT` lets
`-m daemon.tts_daemon` resolve the package regardless of the service's working
directory — required for systemd `--user`, harmless for launchd.)

macOS — install and start via the platform seam:
`uv run python -c "from daemon.platforms import make_platform; make_platform().install_service(program_args=[...], env={...})"`.
This writes `~/Library/LaunchAgents/com.claude-tts.daemon.plist` and
`launchctl bootstrap`s it. Linux — install and start via the same platform seam:
`uv run python -c "from daemon.platforms import make_platform; make_platform().install_service(program_args=[...], env={...})"`.
This renders `~/.config/systemd/user/claude-tts.service`, then runs
`systemctl --user daemon-reload`, `systemctl --user enable --now claude-tts.service`,
and `loginctl enable-linger` (so the daemon survives logout/reboot). If `systemctl`
is absent (no systemd user session), the seam raises a clear message — fall back to
starting the daemon manually (`uv run python -m daemon.tts_daemon &`).

Verify the socket binds at
`${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.
Remediation on failure: check `/tmp/claude-tts-daemon.err.log`.

## Step 7 — Write the config + verify

Write `~/.config/claude-tts/config.json` (read any API key from the env var you
named in Step 4 — never inline the raw key):
`uv run python -c "import os; from daemon.config_io import render_config, write_config; write_config(render_config(engine='<engine>', voice_name='<voice>', backend='<backend>', model='<model>', base_url='<url>', api_key=os.environ.get('<KEY_ENV>', '')))"`.
Then run the `/tts:doctor` checks (disk, daemon+socket, deps, backend
reachability) and play one test utterance to confirm audio. Report the overall
verdict. If doctor reports any WARN, surface it with its remediation.
