---
name: tts:uninstall
description: Stop and remove the claude-tts OS service and daemon, and optionally remove its config. Confirms before deleting.
---

Uninstall the claude-tts **runtime** (service + daemon). This does NOT remove the
plugin itself — plugin removal is done through the Claude Code marketplace.

Steps:

1. **Stop and remove the OS service** — unload and delete the launchd plist on macOS,
   or stop and disable the systemd `--user` unit on Linux.
2. **Kill the daemon** — terminate the running daemon process if present and confirm
   the socket at `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}` is
   gone.
3. **Optionally remove config** — ask whether to delete `~/.config/claude-tts`
   (config + logs). **Confirm explicitly before any destructive deletion.** If the
   user declines, leave it in place.

After finishing, tell the user that to also remove the plugin they should remove the
`claude-tts` marketplace/plugin in Claude Code. Do not hardcode absolute personal
paths.
