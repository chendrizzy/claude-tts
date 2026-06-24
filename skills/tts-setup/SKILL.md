---
name: tts-setup
description: Use for first-run setup of the claude-tts plugin — bootstraps the engine, LLM backend, OS service, and config on a fresh machine. Invoked by /tts:setup.
---

# claude-tts — First-Run Setup (stub)

This skill performs first-run setup for the claude-tts plugin. It is invoked by the
`/tts:setup` command.

## The 7 setup steps

1. **Detect platform/arch** — macOS vs Linux, arm64 vs x86_64.
2. **Ensure `uv` + environment** — install `uv` if absent; create the project venv.
3. **Pick engine + LLM backend** — choose the speech engine and the LLM used for
   content routing, based on what the machine supports.
4. **Calibrate against the mini-eval** — run the bundled mini-eval to tune routing so
   speech is useful, not noisy.
5. **Install the OS service** — launchd plist (macOS) or systemd `--user` unit (Linux)
   so the daemon runs in the background.
6. **Write `~/.config/claude-tts/config.json`** — persist engine, voice, backend, and
   routing settings. Never write absolute personal paths.
7. **Verify** — start the daemon and confirm the socket binds at
   `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.

## Status

**Stub.** Full bootstrap/calibration/service-install logic is implemented in Plan 3c.
For now, report that automated setup is not yet available in this build and direct the
user to `/tts:doctor` to inspect current health.
