---
name: tts:voice
description: Choose the speech engine and voice for claude-tts, then restart the daemon to apply.
---

Help the user choose a **speech engine** and **voice** for the claude-tts daemon.

Steps:

1. Read the current selection from `~/.config/claude-tts/config.json` (the
   `voice.engine` and `voice.voice` fields). If the file does not exist, tell the
   user to run `/tts:setup` first and stop.
2. List the engines available on this machine and, for the selected engine, the
   voices it offers. Present them as a numbered list with a recommendation.
3. When the user picks, update `voice.engine` and/or `voice.voice` in
   `~/.config/claude-tts/config.json`. Preserve all other config keys exactly;
   write valid JSON.
4. Restart the daemon so the change takes effect (the daemon reads config on
   start). Confirm the socket is bound afterward at
   `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`.
5. Speak a one-line confirmation so the user hears the new voice.

Do not hardcode absolute personal paths. If the daemon is not installed, say so and
suggest `/tts:setup`.
