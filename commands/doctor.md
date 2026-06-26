---
name: tts:doctor
description: Diagnose claude-tts — disk, daemon, socket, Python deps, backend reachability, and summarizer budget, with PASS/WARN and remediations. Idempotent.
---

Diagnose the claude-tts installation. This command is **idempotent** — safe to run
anytime, changes nothing. For each check print **PASS** or **WARN** and, on WARN,
exactly one remediation.

Checks:

1. **Disk** — free space on the volume holding the audio temp directory. WARN if
   low; below ~200 MB free the daemon's disk guard evicts cache, refuses to
   synthesize, and fires a loud desktop + statusline alert (it no longer
   silently mutes).
2. **Daemon process + socket** — is the daemon process alive AND is the socket bound
   at `${CLAUDE_TTS_SOCKET:-${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock}`? WARN if the
   process is up but the socket is missing (a stale/orphaned daemon), with a restart
   remediation.
3. **Python deps** — are the daemon's required Python packages importable in its
   environment? WARN with the install command if any are missing.
4. **Backend reachability** — for the configured backend, confirm it is reachable:
   Ollama responding, the Voicebox app running, or edge-tts network access. WARN with
   how to start/restore the backend.
5. **Summarizer budget vs. inner timeout** (Ollama LLM path only) — read
   `summarizer.soft_tokens` (default 200) and `slack_tokens` (default 96) → the hard
   generation ceiling `num_predict = soft + slack` (default 296), and
   `summarizer.inner_timeout_s` (default 5.0). A full-budget summary must finish
   within the inner timeout or it falls back to the deterministic rule-based summary.
   If Ollama is reachable, time a small generation (`num_predict: 64`) against the
   configured model and derive tokens/sec from the response's `eval_count` /
   `eval_duration`; **WARN if** `inner_timeout_s < (num_predict / tok_per_sec) × 1.3`
   (a full-budget summary risks timing out) — remediation: raise `inner_timeout_s`,
   or lower `soft_tokens` / `slack_tokens`. Report the numbers either way (e.g.
   "budget 296 tok ≈ 1.9 s at ~155 tok/s vs 5.0 s timeout — OK"). The bundled defaults
   pass with margin at normal speeds; this mainly catches a budget raised past its
   timeout, or a host too slow for the configured budget. If throughput can't be
   measured, only WARN when the budget exceeds 296 while the timeout is still ≤ 5.0 s.

After all checks, print a one-line overall verdict (all PASS, or the count of WARNs).
Do not hardcode absolute personal paths; read locations from `~/.config/claude-tts`
and the XDG socket expression above.
