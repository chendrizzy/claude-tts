---
name: tts:status
description: Report claude-tts health — daemon socket, active engine and LLM model, and a recent log tail.
---

Report the current health of the claude-tts daemon. This is read-only — do not
change any configuration.

Report:

1. **Daemon socket** — is it bound at
   `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`? Show bound or
   not bound. "Not bound" means the daemon is not running (hooks degrade silently).
2. **Active engine and LLM model** — read `voice.engine` and the configured LLM
   model from `~/.config/claude-tts/config.json`. If the config is missing, say so
   and suggest `/tts:setup`.
3. **Recent log tail** — show the last ~15 lines of the daemon log so the user can
   see recent speech activity or errors.

Keep the output compact and skimmable. For anything failing, point the user at
`/tts:doctor` for a full diagnosis with remediations.
