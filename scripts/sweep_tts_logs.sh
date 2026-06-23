#!/bin/sh
# sweep_tts_logs.sh — idempotent, bounded retention sweep for ~/.claude/logs/tts/
# (DIAGNOSIS R3 hygiene). Purges the residue hook logs (139k+ pre-task-*.log and
# post-tool-*.log written one-file-per-invocation by an UNWIRED archive hook) and
# caps stale, unrotated internal logs.
#
# Safe to run repeatedly and on every SessionStart:
#   * mkdir single-flight lock      -> no concurrent runs
#   * 6h throttle stamp             -> near-zero cost on hot paths
#   * xargs -n 500 batching         -> no ARG_MAX overflow on the 143k purge
#   * NEVER signals/kills the daemon-> launchd KeepAlive can't restart-loop
#   * truncate only stale (>1h idle)-> never races a live file handle
#   * NEVER touches tts_daemon.log* -> daemon-owned (already RotatingFileHandler)
#     or shadow.log                 -> live append + the analysis corpus
# POSIX sh (no flock/bash dependency, works on macOS /bin/sh).
#
# Usage:  sh scripts/sweep_tts_logs.sh [--dry-run] [--force]
#   --dry-run : print what WOULD be removed/truncated; change nothing.
#   --force   : ignore the 6h throttle (initial purge / manual runs).

set -eu

TTS_LOG_DIR="${CLAUDE_TTS_LOG_DIR:-$HOME/.claude/logs/tts}"
RESIDUE_RETENTION_DAYS=3
STALE_CAP_MB=20
STALE_MIN_IDLE_MIN=60          # only truncate files untouched for >1h
THROTTLE_MIN=360               # 6h
DRY_RUN=0
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

[ -d "$TTS_LOG_DIR" ] || { echo "sweep: no $TTS_LOG_DIR — nothing to do"; exit 0; }

# --- anti-storm: single-flight mkdir lock (auto-removed on exit) ---
LOCK="$TTS_LOG_DIR/.sweep.lock"
STALE_LOCK_MIN=15
if ! mkdir "$LOCK" 2>/dev/null; then
  # Stale-lock recovery (self-healing): a real run finishes in seconds, so a
  # lock dir older than STALE_LOCK_MIN was orphaned by a SIGKILL/power-loss
  # (the EXIT trap can't fire on SIGKILL). Reclaim it ONCE, so an interrupted
  # run can never permanently disable the sweep and let residue regrow silently.
  if [ -n "$(find "$LOCK" -maxdepth 0 -type d -mmin +"$STALE_LOCK_MIN" 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null || true
    if ! mkdir "$LOCK" 2>/dev/null; then
      echo "sweep: lock contended after stale reclaim — skipping"
      exit 0
    fi
    echo "sweep: reclaimed stale lock (>${STALE_LOCK_MIN}m old)"
  else
    echo "sweep: another run holds the lock — skipping"
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

# --- throttle: skip if swept within THROTTLE_MIN (unless --force/--dry-run) ---
STAMP="$TTS_LOG_DIR/.last_sweep"
if [ "$FORCE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  if [ -n "$(find "$STAMP" -mmin -"$THROTTLE_MIN" 2>/dev/null)" ]; then
    echo "sweep: throttled (last run < ${THROTTLE_MIN}m ago)"
    exit 0
  fi
fi

echo "sweep: dir=$TTS_LOG_DIR dry_run=$DRY_RUN force=$FORCE"

# --- 1) purge residue hook logs older than N days, in capped batches ---
total_residue=$(find "$TTS_LOG_DIR" -maxdepth 1 -type f \
  \( -name 'pre-task-*.log' -o -name 'post-tool-*.log' \) 2>/dev/null | wc -l | tr -d ' ')
old_residue=$(find "$TTS_LOG_DIR" -maxdepth 1 -type f \
  \( -name 'pre-task-*.log' -o -name 'post-tool-*.log' \) -mtime +"$RESIDUE_RETENTION_DAYS" 2>/dev/null | wc -l | tr -d ' ')
if [ "$DRY_RUN" -eq 1 ]; then
  echo "sweep: [dry-run] would delete $old_residue of $total_residue residue files older than ${RESIDUE_RETENTION_DAYS}d"
else
  find "$TTS_LOG_DIR" -maxdepth 1 -type f \
    \( -name 'pre-task-*.log' -o -name 'post-tool-*.log' \) -mtime +"$RESIDUE_RETENTION_DAYS" -print0 2>/dev/null \
    | xargs -0 -n 500 rm -f 2>/dev/null || true
  remaining=$(find "$TTS_LOG_DIR" -maxdepth 1 -type f \
    \( -name 'pre-task-*.log' -o -name 'post-tool-*.log' \) 2>/dev/null | wc -l | tr -d ' ')
  echo "sweep: residue files $total_residue -> $remaining (deleted >${RESIDUE_RETENTION_DAYS}d old)"
fi

# --- 2) cap stale, UNROTATED internal logs (truncate in place) ---
cap_bytes=$((STALE_CAP_MB * 1024 * 1024))
for name in interrupt_handler.log timeout_manager.log; do
  f="$TTS_LOG_DIR/$name"
  [ -f "$f" ] || continue
  size=$(wc -c < "$f" 2>/dev/null | tr -d ' ')
  [ -n "$size" ] || continue
  if [ "$size" -gt "$cap_bytes" ] && [ -n "$(find "$f" -mmin +"$STALE_MIN_IDLE_MIN" 2>/dev/null)" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "sweep: [dry-run] would truncate $name ($((size/1024/1024))MB, stale >${STALE_MIN_IDLE_MIN}m)"
    else
      : > "$f" && echo "sweep: truncated $name ($((size/1024/1024))MB -> 0)"
    fi
  fi
done

# --- done ---
if [ "$DRY_RUN" -eq 0 ]; then
  touch "$STAMP" 2>/dev/null || true
fi
echo "sweep: complete"
