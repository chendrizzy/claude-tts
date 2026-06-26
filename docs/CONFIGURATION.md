# Configuration

claude-tts reads one JSON config file and a handful of optional environment
variables. **Every block is optional** — the daemon embeds safe defaults
(`daemon/config_io.py`, `daemon/schema_validator.py`), so a missing key or whole
block just falls back. The fastest way to start is to copy the fully-annotated
[`config.example.json`](../config.example.json) and delete what you don't need.

## Where config lives

Resolution order (`daemon/paths.py`):

1. `$CLAUDE_TTS_CONFIG` — explicit full path, if set.
2. otherwise `$XDG_CONFIG_HOME/claude-tts/config.json`, defaulting to
   `~/.config/claude-tts/config.json`.

`/tts:setup` writes this file; `/tts:voice` edits the `voice` block in place. The
directory is created `0700` and the file `0600` (owner-only) — it may hold an
API key, so it is never world-readable.

## The blocks

### `voice` — engine and speech parameters
| Key | Default | Notes |
|-----|---------|-------|
| `engine` | `edge-tts` | `edge-tts` · `kokoro` · `voicebox` · `say` · `espeak` |
| `name` | `en-US-AvaNeural` | voice id; engine-specific (ignored by `say`/`espeak`) |
| `rate` | `1.0` | speed multiplier |
| `volume` | `1.0` | `afplay -v` gain (macOS only; Linux audio daemon owns volume) |
| `mlx_python` | `null` | **kokoro only** — interpreter with `mlx-audio`; `null` → `$MLX_PYTHON`, else `python3` |
| `kokoro_model` | `mlx-community/Kokoro-82M-bf16` | **kokoro only** |

### `llm_provider` — the judge/summarize brain
| Key | Default | Notes |
|-----|---------|-------|
| `type` | `ollama` | `ollama` · `openai` · `null` |

`openai` (any OpenAI-compatible server) also reads `base_url`, `model`, and
`api_key` from this block. `null` is the deterministic, no-LLM floor — TTS still
works, rule-based. See [ARCHITECTURE.md](ARCHITECTURE.md#no-llm-floor).

### `summarizer` — Ollama budget and warmth (used when `type = ollama`)
| Key | Default | Notes |
|-----|---------|-------|
| `model` | `qwen2.5-coder:1.5b` | any Ollama model; setup-time calibration checks it |
| `inner_timeout_s` | `3.5` | **hard cap on one summarize call** — the binding budget |
| `keep_alive` | `30m` | how long Ollama keeps the model resident |
| `warm_interval_s` | `120` | background keep-warm ping interval |

### `routing` — `ContentRouter` timeouts
| Key | Default | Notes |
|-----|---------|-------|
| `summarize_timeout_s` | `4.0` | outer wrapper; **clamped in code to exceed `summarizer.inner_timeout_s`** |
| `binary_judge_timeout_s` | `2.5` | the SPEAK/SKIP judge call |

The clamp matters: if the outer wrapper fired *before* the inner summarize call,
it would cancel summarization and read the **raw markdown** aloud. The code
enforces `wrapper > inner` regardless of what you set here, so this is one knob
you can't misconfigure into a regression.

### `statusline` — what the spoken-output statusline + `/tts:log` *show* (view-only)
| Key | Default | Notes |
|-----|---------|-------|
| `subagent_aware` | `true` | pivot the statusline to whichever agent context spoke most recently **in the same project (cwd)**, so a running sub-agent / background-agent surfaces live (marked 🔊⤷). Scoped by cwd — sub-agents inherit the parent's cwd; an *unrelated* concurrent session in another directory is never followed. `false` → strictly this session's own output |
| `active_window_s` | `90` | how recently (seconds) a same-cwd agent must have spoken for the `subagent_aware` pivot to follow it |
| `include_subagent_in_main` | `false` | `true` → `/tts:log` (no `--session`) merges sibling lines that overlap this session's time span **and ran in the same project (cwd)**, each tagged by source. Same-cwd scoping makes this safe to enable even with multiple projects open |

These flags are **view-only** — the daemon always logs raw per-session truth
(`daemon/spoken_log.py` writes a bounded per-session JSONL under
`~/.claude/logs/tts/spoken/<session>.jsonl`); they only change what the
statusline and `/tts:log` display. Each agent (including sub-agents /
background-agents) gets its own `session_id`, hence its own spoken-log file. The
🔊 statusline **segment** is rendered by an external wrapper that composes your
base statusline with a claude-tts segment and is **not shipped in this repo** —
the repo ships only this config block plus the spoken-log data it reads.

> **Sub-agent following is cwd-scoped (v0.1.5).** A v0.1.4-and-earlier build let
> the statusline pivot to the most-recently-active agent by picking the newest
> spoken-log file in the whole directory; because entries didn't record a cwd,
> two sessions in different directories mirrored each other's output. Now every
> spoken-log entry records the project dir (cwd) it was spoken in, and both the
> merge (`include_subagent_in_main`) and the wrapper's pivot (`subagent_aware`)
> only consider siblings whose cwd matches. Sub-agents and background agents
> inherit their parent's cwd, so they're followed; an unrelated concurrent
> session in another directory is never folded in. Entries that predate the cwd
> field fall back to the old time-only behavior.

### `filtering` — what reaches the router at all
| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `true` | master switch |
| `filter_code_blocks` | `true` | drop fenced code |
| `filter_tool_output` | `true` | apply the tool-output drop filter |
| `filter_technical_output` | `true` | drop listings/diffstats/hashes |
| `max_response_length` | `6500` | longer responses are summarized or skipped |

### `voicebox` — local Voicebox app (used when `engine = voicebox`)
| Key | Default | Notes |
|-----|---------|-------|
| `url` | `http://127.0.0.1:17493` | local REST endpoint |
| `profile_id` | `null` | voice profile; `null` → engine default |
| `engine` | `kokoro` | Voicebox's own synthesis backend |
| `personality` | `false` | **lossy** persona rewrite — keep `false` for faithful status |
| `cleanup` | `true` | delete generated audio from Voicebox history after playback |
| `timeout_s` | `10.0` | REST timeout |

### `audio` — playback and mixing
| Key | Default | Notes |
|-----|---------|-------|
| `global_lock` | `true` | prevent audio overlap across all Claude sessions |
| `playback_device` | `default` | output device |
| `normalize_volume` | `true` | per-chunk normalization |
| `fade_in_ms` / `fade_out_ms` | `50` / `100` | crossfade edges |

### `performance` — caching and concurrency
| Key | Default | Notes |
|-----|---------|-------|
| `cache_enabled` | `true` | reuse synthesized audio for identical text+voice |
| `cache_ttl_seconds` | `3600` | swept on a timer (prevents disk-fill silent-mute) |
| `max_concurrent_requests` | `3` | per-session synthesis workers |
| `timeout_seconds` | `30` | request timeout |
| `retry_attempts` | `2` | synthesis retries |

### `logging`
| Key | Default | Notes |
|-----|---------|-------|
| `level` | `INFO` | `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `log_to_file` | `true` | logs under `~/.claude/logs/tts/` |
| `max_log_size_mb` | `50` | rotation size |
| `keep_logs_days` | `7` | retention (`make sweep-logs`) |

### `advanced` — change only if you mean it
| Key | Default | Notes |
|-----|---------|-------|
| `socket_path` | `/tmp/tts_daemon.sock` | **legacy** — see "Socket path" below |
| `max_queue_size` | `50` | queue depth that trips the BLACK tier |
| `health_check_interval_seconds` | `60` | background health probe |
| `circuit_breaker_enabled` | `true` | trip a failing component instead of looping |
| `circuit_breaker_threshold` | `5` | failures before a component opens |
| `resource_monitoring` | `true` | memory/CPU sampling |

### `feature_flags` — runtime toggles (re-read per request, no restart)
| Key | Default | Notes |
|-----|---------|-------|
| `legacy_speak_enabled` | `true` | keep the legacy speak path available |

## Socket path

The daemon binds the socket resolved by `daemon/paths.py`:

1. `$CLAUDE_TTS_SOCKET` if set, otherwise
2. `${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock`.

To relocate the socket, set `CLAUDE_TTS_SOCKET` — that is the source of truth.
The `advanced.socket_path` field above is retained for compatibility and is
superseded by this resolution; setting the env var is the supported path.

## Disk guard

A just-in-time disk guard (`daemon/pipeline/generate_stage.py`,
`_ensure_disk_space()` / `_signal_disk_full()`) runs before every synthesis
write. If free space on the cache volume is below ~200 MB it first evicts cache
(5-minute age) and re-checks; if still low it refuses to synthesize that chunk
and fires a desktop notification (`osascript` on macOS, `notify-send` on Linux)
plus a `disk_full` spoken-log entry — alerting you instead of silently muting
when the write would later fail on a full volume. The notification needs no disk
write, so it fires even at ~0 bytes free; the alert is throttled to at most once
per minute.

This ~200 MB floor is a **fixed internal threshold** (`MIN_FREE_BYTES_DEFAULT`,
also a `min_free_bytes` constructor arg) — it is **not** exposed in
`config.example.json` and is not config-tunable.

## Environment variables

None are required; none are secrets. See [`.env.example`](../.env.example).

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_TTS_CONFIG` | full path to `config.json` | `~/.config/claude-tts/config.json` |
| `CLAUDE_TTS_SOCKET` | daemon socket path | `${XDG_RUNTIME_DIR:-/tmp}/claude-tts.sock` |
| `CLAUDE_TTS_ENABLED` | global on/off | `true` |
| `CLAUDE_TTS_PASSTHROUGH` | hook stdout passthrough (`hooks/pre-tool-use.sh`, `post-tool-use.sh`); Cursor wrappers set `false` to keep stdout clean | `true` |
| `CLAUDE_TTS_RATE` | speech rate delta | `+20%` |
| `CLAUDE_TTS_PITCH` | pitch shift | `+3Hz` |
| `CLAUDE_TTS_VOICE_STYLE` | edge-tts style | `expressive` |
| `OLLAMA_HOST` | Ollama endpoint | `http://127.0.0.1:11434` |
| `MLX_PYTHON` | interpreter with `mlx-audio` (**kokoro only**) | `python3` on `PATH` |
| `KOKORO_MODEL` | Kokoro model id | `mlx-community/Kokoro-82M-bf16` |
| `KOKORO_VOICE` | Kokoro voice | `af_heart` |
| `KOKORO_LANG` | Kokoro language (`a` US, `b` UK, …) | `a` |
| `CLAUDE_TTS_LAUNCHD_LABEL` | macOS LaunchAgent label | `com.claude-tts.daemon` |
| `CLAUDE_SESSION_ID` | injected by Claude Code at runtime — **not user-set** | — |

Voice-tuning env vars (`CLAUDE_TTS_RATE`/`PITCH`/`VOICE_STYLE`, `KOKORO_*`)
override the matching `config.json` values when set.
