---
name: tts:doctor
description: Diagnose claude-tts — disk, daemon, socket, Python deps, and backend reachability, with PASS/WARN and remediations. Idempotent.
---

Diagnose the claude-tts installation. This command is **idempotent** — safe to run
anytime, changes nothing. For each check print **PASS** or **WARN** and, on WARN,
exactly one remediation.

Checks:

1. **Disk** — free space on the volume holding the audio temp directory. WARN if
   low; a full disk silently mutes every engine (audio can't be written).
2. **Daemon process + socket** — is the daemon process alive AND is the socket bound
   at `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`? WARN if the
   process is up but the socket is missing (a stale/orphaned daemon), with a restart
   remediation.
3. **Python deps** — are the daemon's required Python packages importable in its
   environment? WARN with the install command if any are missing.
4. **Backend reachability** — for the configured backend, confirm it is reachable:
   Ollama responding, the Voicebox app running, or edge-tts network access. WARN with
   how to start/restore the backend.

After all checks, print a one-line overall verdict (all PASS, or the count of WARNs).
Do not hardcode absolute personal paths; read locations from `~/.config/claude-tts`
and the XDG socket expression above.
