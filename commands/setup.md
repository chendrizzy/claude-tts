---
name: tts:setup
description: First-run setup for claude-tts — bootstraps the engine, LLM backend, OS service, and config so Claude can speak.
---

You are running first-run setup for the **claude-tts** plugin.

Invoke the **tts-setup** skill and follow it end-to-end. The skill bootstraps:

1. Detect the platform and architecture (macOS/Linux, arm64/x86_64).
2. Ensure `uv` and a project virtual environment are available.
3. Pick the speech engine and the LLM backend for content routing.
4. Calibrate the chosen backend against the bundled mini-eval.
5. Install the OS background service (launchd on macOS, systemd --user on Linux).
6. Write `~/.config/claude-tts/config.json`.
7. Verify the daemon starts and the socket binds at
   `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.

Run the skill now. Do not hardcode absolute paths — always use `~/.config/claude-tts`
and the XDG socket expression above. If a step cannot complete, report which step
failed and the single most likely remediation, then stop.

Note: the full bootstrap and calibration logic lives in the tts-setup skill. If the
skill reports it is a stub, tell the user setup automation is not yet available in
this build and point them at `/tts:doctor` to check current health.
