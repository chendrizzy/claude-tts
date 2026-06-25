# Changelog

All notable changes to claude-tts are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-06-25

First packaged release: a local text-to-speech daemon for Claude Code, with
smart content routing, hooks, and graceful degradation.

### Added
- Three swappable seams: LLM provider (`ollama` / OpenAI-compatible / `null`),
  TTS engine (`kokoro` / `voicebox` / `edge-tts` / `say` / `espeak`), and OS
  platform (audio playback + service install).
- Zero-dependency fallback engine: macOS `say` / Linux `espeak`, so TTS works
  with no ML or network dependencies.
- Cross-platform audio playback: macOS `afplay`; Linux auto-detects
  `ffplay`/`mpv`/`pw-play`/`paplay`/`aplay` (decoders first, for format safety).
- Background service install: macOS `launchd`, Linux `systemd --user`
  (`enable --now` + `loginctl enable-linger`). Windows points to WSL2/Docker.
- Commands: `/tts:setup` (mini-eval calibration + service install + config),
  `/tts:doctor` (idempotent health checks), `/tts:uninstall`.
- Binding quality gate (`make verify`) — deterministic, no live deps.
- CI matrix (GitHub Actions): macOS + Linux × Python 3.11–3.13.
- Pinned dependencies (`uv.lock`) and version-synced manifests
  (`make manifests`).

### Notes
- Volume control is macOS-only (`afplay -v`); on Linux the audio daemon owns
  system volume.
- Windows service install and Linux/Windows volume are not yet implemented.
