#!/usr/bin/env python3
"""
Production-Grade TTS Daemon Service
Enterprise-ready with comprehensive monitoring and error handling

Features:
- Runs as a persistent background daemon
- Unix domain socket for IPC communication
- Maintains state across all sessions
- Auto-starts when needed
- Production-grade error handling and recovery
- Session-specific voice management
- Intelligent queue management
- Circuit breaker pattern for failing components
- Comprehensive logging with rotation
- Resource monitoring and optimization
- Graceful shutdown with state persistence
"""

import os
import sys
import time
import json
import socket
import threading
import subprocess
import tempfile
import signal
import atexit
import hashlib
import re
import gc
import traceback
import logging
import logging.handlers
import asyncio  # Wave 2: bridge sync handle_client() to async pipeline coroutines
import psutil
import uuid    # OBSERVE-01: mint request_id when hook payload omits event_id
import weakref
from pathlib import Path

# Wave 2 wiring: ensure `from daemon.*` imports work when this file is
# launched directly (`python3 daemon/tts_daemon.py`) rather than as a
# package member. Without this the Wave 2 pipeline modules fail to import
# and the daemon falls back to legacy speak only.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Any, List, Tuple
from dataclasses import dataclass, field, asdict
from collections import deque, defaultdict
from contextlib import contextmanager
import fcntl  # For file locking on Unix systems
from enum import Enum
import random

# Production Constants
from daemon.paths import config_path, socket_path
SOCKET_PATH = socket_path()
PID_FILE = Path.home() / ".claude" / "tts_daemon.pid"
LOG_DIR = Path.home() / ".claude" / "logs" / "tts"


def _pid_is_tts_daemon(pid: int) -> bool:
    """Return True iff `pid` is genuinely a running tts_daemon.py process.

    The singleton guard cannot rely on liveness (os.kill(pid, 0)) alone: the
    kernel reuses PID numbers across reboots, so a recorded PID may now belong
    to an unrelated process. We verify *identity* by inspecting the process
    command line for our daemon's script name. Conservative: any failure to
    introspect returns False (treat as "not our daemon" → stale PID file),
    because a false "alive" is what crash-loops the daemon, whereas a false
    "stale" at worst overwrites a PID file we are about to rewrite anyway.
    """
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
            Exception):
        return False
    return "tts_daemon.py" in cmdline
STATE_FILE = Path.home() / ".claude" / "tts_daemon_state.json"
HEALTH_FILE = Path.home() / ".claude" / "tts_daemon_health.json"
METRICS_FILE = Path.home() / ".claude" / "tts_daemon_metrics.json"

# ---- Build identity (DIAGNOSIS H0: make runtime ≡ repo verifiable) ----
# The health 'uptime_seconds' is cumulative-since-first-ever-launch (daemon_start
# is persisted in STATE_FILE and clobbers the fresh value on every restart), so it
# LIES about this process's age — that is what misled the diagnosis into
# "31-day-old code". We stamp the running code's git SHA (the trustworthy
# identity), a dirty flag, and the newest daemon source mtime (advisory only —
# multi-volume copies bump mtimes) into health so an operator/harness can verify
# exactly which code is live. Computed ONCE at import; git has a 2s timeout.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _compute_build_stamp() -> dict:
    stamp = {"git_sha": "unknown", "git_dirty": None,
             "code_mtime_iso": None, "pkg_version": None}
    try:
        sha = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if sha.returncode == 0:
            stamp["git_sha"] = sha.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        )
        if dirty.returncode == 0:
            stamp["git_dirty"] = bool(dirty.stdout.strip())
    except Exception:
        pass
    try:
        srcs = [
            _REPO_ROOT / "daemon" / "tts_daemon.py",
            _REPO_ROOT / "daemon" / "content_router.py",
            _REPO_ROOT / "daemon" / "text_utils.py",
            _REPO_ROOT / "daemon" / "ollama_summarizer.py",
            _REPO_ROOT / "daemon" / "pipeline" / "process_stage.py",
        ]
        mtimes = [p.stat().st_mtime for p in srcs if p.exists()]
        if mtimes:
            stamp["code_mtime_iso"] = datetime.fromtimestamp(
                max(mtimes)).isoformat(timespec="seconds")
    except Exception:
        pass
    return stamp


_BUILD_STAMP = _compute_build_stamp()

# Performance and Reliability Settings
MAX_MEMORY_MB = 512
MAX_QUEUE_SIZE = 100
HEALTH_CHECK_INTERVAL = 30
METRICS_FLUSH_INTERVAL = 60
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 300
GC_INTERVAL = 300
MAX_CONCURRENT_REQUESTS = 10
REQUEST_TIMEOUT = 30
SOCKET_TIMEOUT = 5

class ComponentState(Enum):
    """Component health states for circuit breaker"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"
    BROKEN = "broken"

class ErrorCategory(Enum):
    """Error categorization for monitoring"""
    NETWORK = "network"
    RESOURCE = "resource"
    TTS_ENGINE = "tts_engine"
    AUDIO_PLAYBACK = "audio_playback"
    SYSTEM = "system"
    VALIDATION = "validation"

@dataclass
class TTSRequest:
    """Structured TTS request with enhanced tracking"""
    source: str
    content: str
    session_id: str = ""
    priority: int = 5
    timestamp: float = field(default_factory=time.time)
    context: str = ""
    request_id: str = field(default_factory=lambda: hashlib.md5(str(time.time()).encode()).hexdigest()[:12])
    retry_count: int = 0
    max_retries: int = 3
    timeout: float = REQUEST_TIMEOUT

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries

    def increment_retry(self):
        self.retry_count += 1

class CircuitBreaker:
    """Circuit breaker pattern for failing components"""
    def __init__(self, failure_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
                 timeout: float = CIRCUIT_BREAKER_TIMEOUT):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = ComponentState.HEALTHY
        self.lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        with self.lock:
            if self.state == ComponentState.BROKEN:
                if time.time() - self.last_failure_time > self.timeout:
                    self.state = ComponentState.DEGRADED
                    self.failure_count = 0
                else:
                    raise Exception("Circuit breaker is OPEN")

            try:
                result = func(*args, **kwargs)
                if self.state == ComponentState.DEGRADED:
                    self.state = ComponentState.HEALTHY
                self.failure_count = 0
                return result
            except Exception as e:
                self.failure_count += 1
                self.last_failure_time = time.time()

                if self.failure_count >= self.failure_threshold:
                    self.state = ComponentState.BROKEN
                elif self.failure_count > self.failure_threshold // 2:
                    self.state = ComponentState.DEGRADED

                raise e

class ResourceMonitor:
    """Resource usage monitoring and optimization"""
    def __init__(self):
        self.process = psutil.Process()
        self.start_time = time.time()
        self.metrics = defaultdict(list)
        self.lock = threading.Lock()

    def get_memory_usage(self) -> float:
        """Get current memory usage in MB"""
        return self.process.memory_info().rss / 1024 / 1024

    def get_cpu_percent(self) -> float:
        """Get current CPU usage percentage"""
        return self.process.cpu_percent()

    def check_memory_limit(self) -> bool:
        """Check if memory usage exceeds limit"""
        return self.get_memory_usage() > MAX_MEMORY_MB

    def collect_metrics(self):
        """Collect current resource metrics"""
        with self.lock:
            timestamp = time.time()
            self.metrics['memory_mb'].append((timestamp, self.get_memory_usage()))
            self.metrics['cpu_percent'].append((timestamp, self.get_cpu_percent()))

            # Keep only last hour of metrics
            cutoff = timestamp - 3600
            for metric_list in self.metrics.values():
                while metric_list and metric_list[0][0] < cutoff:
                    metric_list.pop(0)

    def get_metrics_summary(self) -> Dict:
        """Get summarized metrics"""
        with self.lock:
            summary = {'uptime_seconds': time.time() - self.start_time}

            for metric_name, values in self.metrics.items():
                if values:
                    recent_values = [v[1] for v in values[-10:]]  # Last 10 readings
                    summary[metric_name] = {
                        'current': recent_values[-1] if recent_values else 0,
                        'avg': sum(recent_values) / len(recent_values),
                        'max': max(recent_values),
                        'min': min(recent_values)
                    }

            return summary

class SessionState(Enum):
    """Session lifecycle states"""
    INITIALIZING = "initializing"
    ACTIVE = "active"
    IDLE = "idle"
    STALE = "stale"
    TERMINATED = "terminated"

class Priority(Enum):
    """Request priority levels"""
    LOW = 1
    NORMAL = 5
    HIGH = 8
    URGENT = 10

# LEGACY-04: SessionQueue class deleted (Phase 3). Constants inlined below.
_SESSION_MAX_SESSIONS = 50
_SESSION_STATE_SAVE_INTERVAL = 60
_SESSION_CLEANUP_INTERVAL = 30
_SESSION_STATE_DIR = Path.home() / ".claude" / "session_states"
_VOICE_ASSIGNMENT_FILE = Path.home() / ".claude" / "voice_assignments.json"


# ---------------------------------------------------------------------------
# DELETED: class SessionQueue (LEGACY-04) — 372 lines removed.
# The legacy speak/SessionQueue/speak_text/process_session_queue paths
# are all gone.  self.session_queues remains as an always-empty Dict so
# the status/cleanup code that iterates it is a safe no-op.
# ---------------------------------------------------------------------------
def _recv_line(sock, max_size: int = 1_048_576) -> bytes:
    """Read until newline; raise on size cap. Sync — handle_client
    runs in threading.Thread per socket_server_loop. Hook scripts
    terminate payloads with b'\\n'. On peer-close-before-newline
    with bytes received, return what we have and let json.loads
    decide (log-and-skip posture, see playback_stage.py:320-326)."""
    chunks = []
    total = 0
    while True:
        chunk = sock.recv(8192)
        if not chunk:
            if not chunks:
                return b""
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_size:
            raise ValueError(f"_recv_line exceeded {max_size} bytes")
        if b"\n" in chunk:
            return b"".join(chunks)


class TTSDaemon:
    """
    Persistent TTS Daemon - A REAL service, not a hack!
    """
    
    # Natural voice options
    VOICE_OPTIONS = {
        'conversational': [
            'en-US-AvaMultilingualNeural',
            'en-US-BrianMultilingualNeural',
            'en-US-EmmaMultilingualNeural',
            'en-US-AndrewMultilingualNeural',
        ],
        'professional': [
            'en-US-ChristopherNeural',
            'en-US-AriaNeural',
            'en-US-GuyNeural',
            'en-US-JennyNeural',
        ],
        'friendly': [
            'en-US-AnaNeural',
            'en-US-AvaNeural',
            'en-US-EmmaNeural',
            'en-US-MichelleNeural',
        ]
    }
    
    def __init__(self):
        # Setup structured logging first
        self._setup_logging()

        # Resource monitoring
        self.resource_monitor = ResourceMonitor()

        # Session management (legacy SessionQueue removed — LEGACY-04; dict kept
        # as empty placeholder so status/cleanup code iterates safely as no-op)
        self.session_queues: Dict[str, dict] = {}
        self.queue_lock = threading.RLock()  # Re-entrant lock for nested operations

        # Voice management
        self.session_voices: Dict[str, str] = {}
        self.voice_assignment_lock = threading.Lock()

        # Global audio playback lock to prevent overlapping audio (per daemon instance)
        self.audio_playback_lock = threading.Lock()

        # System-wide audio lock file to prevent overlapping across ALL sessions/processes
        self.global_audio_lock_file = Path.home() / ".claude" / "global_audio.lock"
        self.global_audio_lock_timeout = 30  # Maximum time to wait for global lock

        # Playback state tracking for interrupt capability
        self.current_process = None
        self.current_process_lock = threading.Lock()
        self.interrupt_event = threading.Event()  # Event-based interrupt signaling
        self.current_text = ""
        self.current_session_id = ""
        self.playback_start_time = 0
        self.estimated_duration = 0
        self.is_playing = False
        self.state_callbacks = []
        self.state_callbacks_lock = threading.Lock()

        # Processing threads with weak references
        self.processing_threads: Dict[str, threading.Thread] = {}
        self.stop_signal = threading.Event()
        self.shutdown_timeout = 30.0

        # Socket server
        self.server_socket = None
        self.server_thread = None
        self.client_handlers: Set[threading.Thread] = weakref.WeakSet()

        # Circuit breakers for different components
        self.tts_circuit_breaker = CircuitBreaker()
        self.audio_circuit_breaker = CircuitBreaker()
        self.socket_circuit_breaker = CircuitBreaker()

        # Configuration
        self.tts_enabled = os.environ.get('CLAUDE_TTS_ENABLED', 'true').lower() == 'true'
        self.tts_rate = os.environ.get('CLAUDE_TTS_RATE', '+20%')
        self.tts_pitch = os.environ.get('CLAUDE_TTS_PITCH', '+3Hz')
        self.voice_style = os.environ.get('CLAUDE_TTS_VOICE_STYLE', 'expressive')

        # Enhanced deduplication with TTL
        self.recent_hashes: Dict[str, float] = {}  # hash -> timestamp
        self.hash_expiry = 10.0
        self.hash_cleanup_time = time.time()

        # TRUE start time of THIS process. Kept OUT of self.stats so load_state()
        # (which updates self.stats from the persisted state file) cannot clobber
        # it — that clobber is exactly why stats['daemon_start'] reports a
        # cumulative-since-first-launch value and health.uptime_seconds "lies".
        # health.process_uptime_seconds reads this for an honest process age (H0).
        self.start_time = time.time()

        # Comprehensive statistics
        self.stats = {
            'daemon_start': time.time(),
            'requests_received': 0,
            'requests_processed': 0,
            'requests_failed': 0,
            'duplicates_prevented': 0,
            'sessions_active': 0,
            'daemon_restarts': 0,
            'circuit_breaker_trips': 0,
            'memory_cleanups': 0,
            'health_checks': 0
        }

        # Error tracking by category
        self.error_counts: Dict[ErrorCategory, int] = defaultdict(int)
        self.last_errors: Dict[ErrorCategory, List[Tuple[float, str]]] = defaultdict(list)

        # Background maintenance threads
        self.maintenance_threads: List[threading.Thread] = []

        # Wave 2 wiring: pipeline singletons. Instantiated in run() so the
        # PID-singleton check fires before we spin up the async loop.
        # Stays None until run() — handle_client() checks readiness before use.
        self._pipeline_adapter = None        # daemon/pipeline/adapter.py:PipelineAdapter
        self._ollama_client = None           # daemon/ollama_integration.py:OllamaClient
        self._ollama_summarizer = None       # daemon/ollama_summarizer.py:OllamaSummarizer
        self._content_router = None          # daemon/content_router.py:ContentRouter
        self._queue_manager = None           # daemon/pipeline/queue_manager.py:QueueManager
        self._tts_user_config: dict = {}     # parsed config/tts_user_config.json (empty if absent)

        # OBSERVE-02: timestamp of the most recent inbound tool_event,
        # for `health` endpoint and ensure-daemon-ready.sh staleness.
        # ``None`` until the first tool_event arrives.
        self._last_tool_event_at: Optional[float] = None

        # Load persistent state
        self.load_state()

        # Load voice assignments (if this method exists)
        try:
            self.session_voices = self._load_voice_assignments()
        except AttributeError:
            pass  # Method doesn't exist in current implementation

        # Start background tasks
        self._start_maintenance_tasks()

        self.logger.info(f"TTS Daemon initialized - PID: {os.getpid()}")
        self.logger.info(f"Socket path: {SOCKET_PATH}")
        self.logger.info(f"Memory limit: {MAX_MEMORY_MB}MB")
    
    def _setup_logging(self):
        """Setup comprehensive structured logging"""
        self.log_dir = LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Main logger
        self.logger = logging.getLogger('tts_daemon')
        self.logger.setLevel(logging.DEBUG)

        # Remove existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

        # Rotating file handler for main log
        main_log = self.log_dir / "tts_daemon.log"
        main_handler = logging.handlers.RotatingFileHandler(
            main_log, maxBytes=10*1024*1024, backupCount=5  # 10MB files, 5 backups
        )
        main_formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d [%(levelname)8s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        main_handler.setFormatter(main_formatter)
        self.logger.addHandler(main_handler)

        # Error-only log
        error_log = self.log_dir / "tts_daemon_errors.log"
        error_handler = logging.handlers.RotatingFileHandler(
            error_log, maxBytes=5*1024*1024, backupCount=3
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(main_formatter)
        self.logger.addHandler(error_handler)

        # Performance log
        perf_log = self.log_dir / "tts_daemon_performance.log"
        self.perf_logger = logging.getLogger('tts_daemon.performance')
        self.perf_logger.setLevel(logging.INFO)
        perf_handler = logging.handlers.RotatingFileHandler(
            perf_log, maxBytes=5*1024*1024, backupCount=3
        )
        perf_formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d PERF: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        perf_handler.setFormatter(perf_formatter)
        self.perf_logger.addHandler(perf_handler)

        # Console handler for stderr
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(
            '[%(levelname)s] TTS-Daemon: %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

        # OBSERVE-01: pipeline modules use `logging.getLogger(__name__)`
        # which gives names like `daemon.pipeline.queue_manager` — these are
        # SIBLING loggers to `tts_daemon`, not children. To capture their
        # request_id traces in the same log file, attach the same file handler
        # to the `daemon` logger so all `daemon.*` child loggers propagate to it.
        pipeline_logger = logging.getLogger('daemon')
        pipeline_logger.setLevel(logging.INFO)
        # Avoid duplicate handlers on daemon restart.
        for h in pipeline_logger.handlers[:]:
            pipeline_logger.removeHandler(h)
        pipeline_logger.addHandler(main_handler)
        pipeline_logger.propagate = False  # don't double-log via root

    @contextmanager
    def global_audio_lock(self):
        """System-wide audio lock to prevent overlapping across ALL Claude Code sessions"""
        lock_file = None
        try:
            # Ensure lock directory exists
            self.global_audio_lock_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Open lock file
            lock_file = open(self.global_audio_lock_file, 'w')
            
            # Try to acquire exclusive lock with timeout
            self.log(f"Acquiring global audio lock...")
            start_time = time.time()
            
            while True:
                try:
                    # Non-blocking exclusive lock
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.log(f"Acquired global audio lock successfully")
                    break
                except IOError:
                    # Lock is held by another process
                    if time.time() - start_time > self.global_audio_lock_timeout:
                        raise TimeoutError(f"Could not acquire global audio lock within {self.global_audio_lock_timeout}s")
                    
                    # Wait a bit and try again
                    time.sleep(0.1)
            
            # Write our PID to the lock file for debugging
            lock_file.write(f"{os.getpid()}\n{time.time()}\n")
            lock_file.flush()
            
            yield
            
        finally:
            if lock_file:
                try:
                    # Release the lock
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()
                    self.log(f"Released global audio lock")
                    
                    # Clean up lock file if we can (not critical if it fails)
                    try:
                        self.global_audio_lock_file.unlink()
                    except:
                        pass  # Another process might have cleaned it up
                        
                except Exception as e:
                    self.log(f"Error releasing global audio lock: {e}")

    def log_error(self, category: ErrorCategory, message: str, exception: Exception = None):
        """Log categorized errors with tracking"""
        self.error_counts[category] += 1
        error_entry = (time.time(), message)
        self.last_errors[category].append(error_entry)

        # Keep only last 10 errors per category
        if len(self.last_errors[category]) > 10:
            self.last_errors[category].pop(0)

        if exception:
            self.logger.error(f"[{category.value}] {message}: {exception}", exc_info=True)
        else:
            self.logger.error(f"[{category.value}] {message}")

    def log(self, message: str):
        """Enhanced logging with timestamp - kept for backward compatibility"""
        self.logger.info(message)

    def _start_maintenance_tasks(self):
        """Start background maintenance tasks"""
        # Resource monitoring task
        monitor_thread = threading.Thread(
            target=self._resource_monitoring_loop,
            daemon=True,
            name="ResourceMonitor"
        )
        monitor_thread.start()
        self.maintenance_threads.append(monitor_thread)

        # Health check task
        health_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name="HealthChecker"
        )
        health_thread.start()
        self.maintenance_threads.append(health_thread)

        # Metrics flush task
        metrics_thread = threading.Thread(
            target=self._metrics_flush_loop,
            daemon=True,
            name="MetricsFlush"
        )
        metrics_thread.start()
        self.maintenance_threads.append(metrics_thread)

        # Garbage collection task
        gc_thread = threading.Thread(
            target=self._garbage_collection_loop,
            daemon=True,
            name="GarbageCollector"
        )
        gc_thread.start()
        self.maintenance_threads.append(gc_thread)

    def _resource_monitoring_loop(self):
        """Background resource monitoring"""
        while not self.stop_signal.is_set():
            try:
                self.resource_monitor.collect_metrics()

                # Check memory limit
                if self.resource_monitor.check_memory_limit():
                    self.logger.warning(f"Memory usage exceeded limit: {self.resource_monitor.get_memory_usage():.1f}MB")
                    self._trigger_memory_cleanup()

                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                self.log_error(ErrorCategory.SYSTEM, "Resource monitoring failed", e)
                time.sleep(60)  # Back off on errors

    def _health_check_loop(self):
        """Background health checking"""
        while not self.stop_signal.is_set():
            try:
                self._perform_health_check()
                self.stats['health_checks'] += 1
                time.sleep(HEALTH_CHECK_INTERVAL)
            except Exception as e:
                self.log_error(ErrorCategory.SYSTEM, "Health check failed", e)
                time.sleep(60)

    def _metrics_flush_loop(self):
        """Background metrics flushing"""
        while not self.stop_signal.is_set():
            try:
                self._flush_metrics()
                time.sleep(METRICS_FLUSH_INTERVAL)
            except Exception as e:
                self.log_error(ErrorCategory.SYSTEM, "Metrics flush failed", e)
                time.sleep(120)

    def _garbage_collection_loop(self):
        """Background garbage collection"""
        while not self.stop_signal.is_set():
            try:
                # Force garbage collection
                collected = gc.collect()
                if collected > 0:
                    self.logger.debug(f"Garbage collected {collected} objects")
                    self.stats['memory_cleanups'] += 1

                time.sleep(GC_INTERVAL)
            except Exception as e:
                self.log_error(ErrorCategory.SYSTEM, "Garbage collection failed", e)
                time.sleep(300)

    def _trigger_memory_cleanup(self):
        """Trigger aggressive memory cleanup"""
        try:
            # Clean up stale sessions
            self.cleanup_stale_sessions()

            # Clean hash cache
            self._cleanup_hash_cache()

            # Force garbage collection
            gc.collect()

            # Log memory usage
            memory_mb = self.resource_monitor.get_memory_usage()
            self.logger.info(f"Memory cleanup completed. Current usage: {memory_mb:.1f}MB")

        except Exception as e:
            self.log_error(ErrorCategory.RESOURCE, "Memory cleanup failed", e)

    def _cleanup_hash_cache(self):
        """Clean expired hashes from cache"""
        current_time = time.time()
        expired_hashes = [
            hash_key for hash_key, timestamp in self.recent_hashes.items()
            if current_time - timestamp > self.hash_expiry
        ]

        for hash_key in expired_hashes:
            del self.recent_hashes[hash_key]

        if expired_hashes:
            self.logger.debug(f"Cleaned {len(expired_hashes)} expired hashes")

    def _perform_health_check(self):
        """Perform comprehensive health check"""
        health_status = {
            'timestamp': time.time(),
            'status': 'healthy',
            'checks': {}
        }

        try:
            # Check socket health
            health_status['checks']['socket'] = {
                'status': 'healthy' if self.server_socket else 'broken',
                'active_connections': len(self.client_handlers)
            }

            # Check memory usage
            memory_mb = self.resource_monitor.get_memory_usage()
            health_status['checks']['memory'] = {
                'status': 'healthy' if memory_mb < MAX_MEMORY_MB else 'warning',
                'usage_mb': memory_mb,
                'limit_mb': MAX_MEMORY_MB
            }

            # Check circuit breakers
            health_status['checks']['circuit_breakers'] = {
                'tts_engine': self.tts_circuit_breaker.state.value,
                'audio_playback': self.audio_circuit_breaker.state.value,
                'socket': self.socket_circuit_breaker.state.value
            }

            # Check session health
            active_sessions = len(self.session_queues)
            health_status['checks']['sessions'] = {
                'active_count': active_sessions,
                'status': 'healthy' if active_sessions < 50 else 'warning'
            }

            # Overall status
            if any(check.get('status') == 'broken' for check in health_status['checks'].values()):
                health_status['status'] = 'broken'
            elif any(check.get('status') == 'warning' for check in health_status['checks'].values()):
                health_status['status'] = 'warning'

            # Write health status
            with open(HEALTH_FILE, 'w') as f:
                json.dump(health_status, f, indent=2)

        except Exception as e:
            health_status['status'] = 'broken'
            health_status['error'] = str(e)
            self.log_error(ErrorCategory.SYSTEM, "Health check failed", e)

    def _flush_metrics(self):
        """Flush metrics to disk"""
        try:
            metrics = {
                'timestamp': time.time(),
                'daemon_stats': self.get_stats(),
                'resource_metrics': self.resource_monitor.get_metrics_summary(),
                'error_counts': {k.value if hasattr(k, 'value') else str(k): v for k, v in self.error_counts.items()},
                'circuit_breaker_states': {
                    'tts_engine': {
                        'state': self.tts_circuit_breaker.state.value,
                        'failure_count': self.tts_circuit_breaker.failure_count
                    },
                    'audio_playback': {
                        'state': self.audio_circuit_breaker.state.value,
                        'failure_count': self.audio_circuit_breaker.failure_count
                    },
                    'socket': {
                        'state': self.socket_circuit_breaker.state.value,
                        'failure_count': self.socket_circuit_breaker.failure_count
                    }
                },
                'session_metrics': {}  # Legacy SessionQueue removed (LEGACY-04)
            }

            with open(METRICS_FILE, 'w') as f:
                json.dump(metrics, f, indent=2)

        except Exception as e:
            self.log_error(ErrorCategory.SYSTEM, "Metrics flush failed", e)
    
    def save_state(self):
        """Save daemon state to disk"""
        state = {
            'stats': self.stats,
            'session_voices': self.session_voices,
            'sessions': {}  # Legacy SessionQueue removed (LEGACY-04)
        }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.log(f"Failed to save state: {e}")
    
    def load_state(self):
        """Load daemon state from disk"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.stats.update(state.get('stats', {}))
                    self.session_voices = state.get('session_voices', {})
                    self.stats['daemon_restarts'] = self.stats.get('daemon_restarts', 0) + 1
                    self.log(f"Loaded state from previous daemon session")
            except Exception as e:
                self.log(f"Failed to load state: {e}")
    
    def start_socket_server(self):
        """Start Unix domain socket server"""
        # Remove old socket if exists
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        
        # Create socket
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(SOCKET_PATH)
        self.server_socket.listen(5)
        
        # Make socket accessible
        os.chmod(SOCKET_PATH, 0o666)
        
        self.log(f"Socket server listening on {SOCKET_PATH}")
        
        # Start server thread
        self.server_thread = threading.Thread(target=self.socket_server_loop, daemon=True)
        self.server_thread.start()
    
    def socket_server_loop(self):
        """Main socket server loop"""
        while not self.stop_signal.is_set():
            try:
                # Accept connection with timeout
                self.server_socket.settimeout(1.0)
                try:
                    client_socket, _ = self.server_socket.accept()
                except socket.timeout:
                    continue
                
                # Handle client in separate thread
                thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_socket,),
                    daemon=True
                )
                thread.start()
                
            except Exception as e:
                if not self.stop_signal.is_set():
                    self.log(f"Socket server error: {e}")
    
    def handle_client(self, client_socket: socket.socket):
        """Handle client connection"""
        try:
            # Receive data — accumulate until newline; handles payloads >4 KB
            data = _recv_line(client_socket).decode('utf-8').rstrip('\n')
            if not data:
                return
            
            # Parse request
            request_data = json.loads(data)
            command = request_data.get('command')
            
            response = {'status': 'error', 'message': 'Unknown command'}

            # LEGACY-03: 'speak' command handler deleted (Phase 3).
            # Any client sending 'speak' now receives the default Unknown command
            # error response.  The new pipeline uses 'tool_event'/'stop_event'.

            # Record the session's cwd (project dir) from the inbound event so
            # spoken_log.append can stamp each entry, enabling cwd-scoped
            # sub-agent following in read_merged() + the statusline (sub-agents
            # inherit the parent's cwd; an unrelated session has a different one).
            # Best-effort — never let it break event handling.
            if command in ('tool_event', 'stop_event') and isinstance(request_data, dict):
                try:
                    from daemon import spoken_log as _spoken_log
                    _spoken_log.note_session_cwd(
                        request_data.get('session_id') or '', request_data.get('cwd')
                    )
                except Exception:
                    pass

            if command == 'tool_event':
                # Wave 2 W2.A: route tool_event payloads through ContentRouter.
                # OBSERVE-03: schema validation BEFORE dispatch — malformed
                # payloads are rejected with a structured `schema_violation`
                # log entry rather than silently classified as silence.
                schema_response = self._reject_if_schema_violation(
                    request_data, 'tool_event',
                )
                if schema_response is not None:
                    response = schema_response
                else:
                    response = self._handle_tool_event(request_data)

            elif command == 'stop_event':
                # Wave 2 W2.A: Stop-hook handler. content is the latest assistant
                # message extracted from transcript_path by speech_output_hook.sh
                # (Wave 3 hook). Always notifies QueueManager.on_stop_hook to
                # flush TurnBuffer (when W2.C lands) regardless of whether the
                # content itself routes to speech.
                # OBSERVE-03: schema validation gate — see tool_event branch.
                schema_response = self._reject_if_schema_violation(
                    request_data, 'stop_event',
                )
                if schema_response is not None:
                    response = schema_response
                else:
                    response = self._handle_stop_event(request_data)

            elif command == 'status':
                # Return enhanced daemon status
                response = {
                    'status': 'success',
                    'stats': self.get_stats(),
                    'sessions': [],
                    'session_details': {},
                    'voice_assignments': self.session_voices,
                    'resource_usage': {
                        'active_sessions': 0,
                        'max_sessions': _SESSION_MAX_SESSIONS,
                        'active_threads': len([t for t in self.processing_threads.values() if t.is_alive()])
                    }
                }

            elif command == 'session_info':
                response = {
                    'status': 'error',
                    'message': 'Legacy session_info not supported (LEGACY-04)'
                }
                
            elif command == 'cleanup':
                # Force cleanup of stale sessions
                self.cleanup_stale_sessions()
                response = {
                    'status': 'success',
                    'message': 'Session cleanup completed',
                    'active_sessions': len(self.session_queues)
                }

            elif command == 'health':
                # Health check endpoint for monitoring
                try:
                    memory_mb = self.resource_monitor.get_memory_usage()
                    cpu_percent = self.resource_monitor.get_cpu_percent()

                    health_status = "healthy"
                    if memory_mb > MAX_MEMORY_MB:
                        health_status = "warning"
                    if any(cb.state == ComponentState.BROKEN for cb in
                           [self.tts_circuit_breaker, self.audio_circuit_breaker, self.socket_circuit_breaker]):
                        health_status = "broken"

                    # OBSERVE-02: expose last_audio_played_at + derived
                    # seconds_since_last_audio so ensure-daemon-ready.sh
                    # can detect silent-session-death (audio stops while
                    # hooks keep firing). Pull from PlaybackStage if the
                    # pipeline is wired; otherwise None.
                    last_audio_at = self._get_last_audio_played_at()
                    now = time.time()
                    seconds_since_last_audio = (
                        (now - last_audio_at) if last_audio_at else None
                    )
                    # Tier reflects current QueueManager pressure (default
                    # session) — exposed so the operator can see whether
                    # the pipeline is in BLACK/RED while audio is silent.
                    tier = self._tier_value('default')
                    last_tool_event_at = getattr(
                        self, '_last_tool_event_at', None,
                    )
                    seconds_since_last_tool_event = (
                        (now - last_tool_event_at)
                        if last_tool_event_at else None
                    )

                    response = {
                        'status': 'success',
                        'health': health_status,
                        'memory_mb': memory_mb,
                        'memory_limit_mb': MAX_MEMORY_MB,
                        'cpu_percent': cpu_percent,
                        'uptime_seconds': time.time() - self.stats['daemon_start'],
                        # H0: process_uptime_seconds is the TRUE age of THIS
                        # process (uptime_seconds above is cumulative-since-first
                        # -launch and lies). 'build' identifies the running code
                        # so on-disk fixes can be confirmed live (runtime ≡ repo).
                        'process_uptime_seconds': time.time() - getattr(
                            self, 'start_time', self.stats.get('daemon_start', time.time())),
                        'build': _BUILD_STAMP,
                        'active_sessions': len(self.session_queues),
                        'circuit_breakers': {
                            'tts_engine': self.tts_circuit_breaker.state.value,
                            'audio_playback': self.audio_circuit_breaker.state.value,
                            'socket': self.socket_circuit_breaker.state.value
                        },
                'error_counts': {k.value if hasattr(k, 'value') else str(k): v for k, v in self.error_counts.items()},
                        'last_health_check': self.stats.get('health_checks', 0),
                        # ---- OBSERVE-02 fields ----
                        'last_audio_played_at': last_audio_at,
                        'seconds_since_last_audio': seconds_since_last_audio,
                        'tier': tier,
                        'last_tool_event_at': last_tool_event_at,
                        'seconds_since_last_tool_event': seconds_since_last_tool_event,
                    }
                except Exception as e:
                    response = {
                        'status': 'error',
                        'message': f'Health check failed: {str(e)}',
                        'health': 'broken'
                    }

            elif command == 'metrics':
                # Detailed metrics endpoint
                try:
                    response = {
                        'status': 'success',
                        'metrics': {
                            'daemon_stats': self.get_stats(),
                            'resource_metrics': self.resource_monitor.get_metrics_summary(),
                            'error_summary': {k.value if hasattr(k, 'value') else str(k): v for k, v in self.error_counts.items()},
                            'circuit_breaker_states': {
                                'tts_engine': {
                                    'state': self.tts_circuit_breaker.state.value,
                                    'failure_count': self.tts_circuit_breaker.failure_count,
                                    'last_failure': self.tts_circuit_breaker.last_failure_time
                                },
                                'audio_playback': {
                                    'state': self.audio_circuit_breaker.state.value,
                                    'failure_count': self.audio_circuit_breaker.failure_count,
                                    'last_failure': self.audio_circuit_breaker.last_failure_time
                                },
                                'socket': {
                                    'state': self.socket_circuit_breaker.state.value,
                                    'failure_count': self.socket_circuit_breaker.failure_count,
                                    'last_failure': self.socket_circuit_breaker.last_failure_time
                                }
                            },
                            'session_metrics': {
                                sid: queue.to_dict() for sid, queue in self.session_queues.items()
                            }
                        }
                    }
                except Exception as e:
                    response = {
                        'status': 'error',
                        'message': f'Metrics collection failed: {str(e)}'
                    }

            elif command == 'interrupt':
                # Interrupt current TTS playback
                force = request_data.get('force', True)
                stopped = self.stop_playback(force=force)

                if stopped:
                    response = {
                        'status': 'success',
                        'message': 'Playback interrupted successfully'
                    }
                else:
                    response = {
                        'status': 'error',
                        'message': 'No playback to interrupt'
                    }

            elif command == 'playback_state':
                # Get current playback state
                state = self.get_playback_state()
                response = {
                    'status': 'success',
                    'playback_state': state
                }

            elif command == 'shutdown':
                # Initiate graceful shutdown with session migration
                self.logger.info("Shutdown command received - preparing for graceful shutdown")
                # Prepare all viable sessions for migration
                migrated_sessions = 0
                try:
                    for session_id in list(self.session_queues.keys()):
                        if hasattr(self, 'migrate_session_to_new_daemon') and self.migrate_session_to_new_daemon(session_id):
                            migrated_sessions += 1
                except AttributeError:
                    self.logger.warning("Session migration not available in this version")

                self.stop_signal.set()
                response = {
                    'status': 'success',
                    'message': 'Shutting down gracefully',
                    'migrated_sessions': migrated_sessions
                }
            
            # Send response
            client_socket.send(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            self.log(f"Error handling client: {e}")
            error_response = {'status': 'error', 'message': str(e)}
            try:
                client_socket.send(json.dumps(error_response).encode('utf-8'))
            except:
                pass
        finally:
            client_socket.close()

    # ------------------------------------------------------------------
    # Wave 2 W2.A — tool_event / stop_event endpoint handlers.
    # Run on the socket-handler thread; bridge to async pipeline via
    # _run_pipeline_coroutine() which uses run_coroutine_threadsafe.
    # ------------------------------------------------------------------

    def _reject_if_schema_violation(
        self, request_data: dict, schema_name: str,
    ) -> Optional[dict]:
        """OBSERVE-03 gate.

        Validate ``request_data`` against the named schema. On violation,
        emit a single-line structured log entry tagged ``schema_violation``
        and return an error response dict — the caller short-circuits the
        normal handler path so the malformed event is REJECTED, not
        silently classified-as-silence (which is what ContentRouter would
        do for missing-shape payloads pre-fix).

        Returns:
            None if valid (caller proceeds with normal dispatch).
            A response dict (status='error') if invalid.
        """
        try:
            from daemon.schema_validator import validate_event
        except Exception as e:  # pragma: no cover — import failure is fatal
            self.log(f"schema_validator import failed: {e}")
            return None  # fail-open: don't break the daemon if validator is broken

        try:
            ok, violations = validate_event(request_data, schema_name)
        except Exception as e:  # pragma: no cover — bug in validator
            self.log(f"schema_validator crash on {schema_name}: {e}")
            return None  # fail-open

        if ok:
            return None

        # Single-line JSON entry — easy to grep / pipe to jq.
        event_id = request_data.get('event_id', '') if isinstance(request_data, dict) else ''
        try:
            entry = json.dumps({
                'event': 'schema_violation',
                'schema': schema_name,
                'event_id': event_id,
                'violations': violations,
                'tool_name': request_data.get('tool_name') if isinstance(request_data, dict) else None,
            }, default=str)
        except Exception:
            entry = f"schema_violation schema={schema_name} violations={violations!r}"
        try:
            self.logger.warning(entry)
        except Exception:
            # Bare-bones fallback: write to stderr so the entry is never lost.
            print(entry, file=sys.stderr)

        return {
            'status': 'error',
            'event_id': event_id,
            'category': None,
            'tier': None,
            'queued': False,
            'reason': f'schema_violation: {"; ".join(violations[:3])}',
        }

    def _handle_tool_event(self, request_data: dict) -> dict:
        """Route a tool_event payload through ContentRouter → QueueManager.

        ERROR category bypasses TurnBuffer via QueueManager.submit_priority
        (queue-jump + optional SIGTERM mid-segment). Other categories go
        through TurnBuffer when W2.C lands; until then we fall through to
        direct PipelineAdapter submission so the path is exercised end-to-end.

        Shadow mode (Wave 2.5): if request_data['shadow'] is true, classify
        and log the decision to ~/.claude/logs/tts/shadow.log, then return
        status='shadowed' WITHOUT submitting to the pipeline. Hooks dual-write
        legacy + shadowed-new during the 24h shadow soak so both paths can be
        compared offline.

        OBSERVE-01: extract ``event_id`` (or mint via uuid4 when absent)
        as the canonical ``request_id`` and log a single line at entry so
        a grep over tts_daemon.log returns ≥1 hit at the dispatch boundary.
        The ID is mutated INTO ``request_data`` so that ContentRouter's
        ``RouterDecision.source_event_id`` picks it up and downstream
        stages (QueueManager, PlaybackStage) inherit it.
        """
        # OBSERVE-01: ensure every event has a request_id. Mint if absent.
        event_id = request_data.get('event_id') if isinstance(request_data, dict) else None
        minted = False
        if not event_id:
            event_id = str(uuid.uuid4())
            minted = True
            if isinstance(request_data, dict):
                request_data['event_id'] = event_id
        # Single-line entry — grep `request_id=<uuid>` to follow the trail.
        try:
            tool_name = request_data.get('tool_name') if isinstance(request_data, dict) else None
            self.logger.info(
                f"tool_event dispatch request_id={event_id} tool={tool_name} "
                f"request_id_minted={'true' if minted else 'false'}"
            )
        except Exception:
            pass
        # OBSERVE-02: stamp the last tool_event timestamp so the `health`
        # endpoint can report seconds_since_last_tool_event.  Used by
        # ensure-daemon-ready.sh to determine whether a session is
        # currently active (any tool_event in the last 60s) before
        # warning about audio staleness.
        self._last_tool_event_at = time.time()
        is_shadow = bool(request_data.get('shadow', False))

        # Pipeline must be ready. If not, fail loud so tests catch it.
        if self._content_router is None or self._queue_manager is None:
            return {
                'status': 'error',
                'event_id': event_id,
                'category': None,
                'tier': None,
                'queued': False,
                'reason': 'pipeline not initialized (ContentRouter/QueueManager missing)',
            }

        # Run the async router under the pipeline event loop.
        try:
            # Local imports to avoid importing pipeline modules at daemon
            # startup if Wave 2 init failed.
            from daemon.tts_types import Category as _Category
            result = self._run_pipeline_coroutine(
                self._content_router.route(request_data), timeout=5.0,
            )
        except Exception as e:
            self.log(f"tool_event router error: {e}")
            self._log_shadow_decision('tool_event', request_data, None, error=str(e), is_shadow=is_shadow)
            return {
                'status': 'error',
                'event_id': event_id,
                'category': None,
                'tier': None,
                'queued': False,
                'reason': f'router error: {e}',
            }

        # Always log to shadow.log — useful audit trail even outside shadow mode.
        self._log_shadow_decision('tool_event', request_data, result, is_shadow=is_shadow)

        # Shadow mode: return classification without submitting to playback.
        if is_shadow:
            return {
                'status': 'shadowed',
                'event_id': result.decision.source_event_id if result else event_id,
                'category': result.decision.category.value if result else None,
                'tier': self._tier_value(request_data.get('session_id') or 'default'),
                'queued': False,
                'reason': 'shadow mode — classified but not submitted',
            }

        # Drop verdict — silence default.
        if result is None:
            return {
                'status': 'skipped',
                'event_id': event_id,
                'category': None,
                'tier': self._tier_value(request_data.get('session_id') or 'default'),
                'queued': False,
                'reason': 'silence default',
            }

        session_id = result.session_id or 'default'
        category = result.decision.category

        # ERROR pre-empts via QueueManager (queue-jump + SIGTERM mid-segment).
        if category == _Category.ERROR:
            try:
                self._run_pipeline_coroutine(
                    self._queue_manager.submit_priority(result), timeout=5.0,
                )
                # ERROR also has to actually become an audible utterance —
                # submit_priority queues the audio jump but doesn't generate.
                # Push the content through the pipeline at PRIORITY_ERROR.
                # OBSERVE-01: thread request_id (source_event_id from
                # ContentRouter) through to PipelineAdapter for traceability.
                self._submit_to_pipeline(
                    result.decision.content, session_id, result.decision.priority,
                    request_id=result.decision.source_event_id or event_id,
                )
                queued = True
                reason = 'error pre-empted'
            except Exception as e:
                self.log(f"tool_event ERROR pre-empt failed: {e}")
                return {
                    'status': 'error',
                    'event_id': result.decision.source_event_id or event_id,
                    'category': category.value,
                    'tier': self._tier_value(session_id),
                    'queued': False,
                    'reason': f'submit_priority failed: {e}',
                }
        else:
            # INSIGHT / STATUS — go through TurnBuffer (W2.C). Until W2.C
            # lands, fall through to direct submission via PipelineAdapter.
            turn_buffer_for = getattr(self._content_router, 'turn_buffer_for', None)
            if callable(turn_buffer_for):
                try:
                    self._run_pipeline_coroutine(
                        turn_buffer_for(session_id).add(result), timeout=5.0,
                    )
                    queued = True
                    reason = 'buffered'
                except Exception as e:
                    self.log(f"tool_event TurnBuffer.add failed: {e}")
                    queued = False
                    reason = f'turn_buffer error: {e}'
            else:
                # Pre-W2.C fallback: submit directly to the pipeline.
                # OBSERVE-01: forward request_id for end-to-end traceability.
                ok = self._submit_to_pipeline(
                    result.decision.content, session_id, result.decision.priority,
                    request_id=result.decision.source_event_id or event_id,
                )
                queued = bool(ok)
                reason = 'submitted directly (TurnBuffer not yet available)' if ok else 'pipeline submit failed'

        return {
            'status': 'accepted' if queued else 'error',
            'event_id': result.decision.source_event_id or event_id,
            'category': category.value,
            'tier': self._tier_value(session_id),
            'queued': queued,
            'reason': reason,
        }

    def _handle_stop_event(self, request_data: dict) -> dict:
        """Route a stop_event payload through ContentRouter and notify QueueManager.

        FINAL_ANSWER goes direct (no batching). INSIGHT goes the same way
        (assistant insights at end-of-turn shouldn't be deferred).
        QueueManager.on_stop_hook is always called so it can flush the
        TurnBuffer (W2.C) and reset its skipped-count for the next turn.

        Shadow mode (Wave 2.5): if request_data['shadow'] is true, classify
        and log the decision to ~/.claude/logs/tts/shadow.log, then return
        status='shadowed' WITHOUT submitting to the pipeline AND without
        touching QueueManager state (so legacy traffic isn't disturbed).

        OBSERVE-01: extract or mint ``event_id`` as the canonical
        ``request_id`` and log a single line so the dispatch boundary is
        visible in tts_daemon.log.
        """
        # OBSERVE-01: ensure request_id exists.
        event_id = request_data.get('event_id') if isinstance(request_data, dict) else None
        minted = False
        if not event_id:
            event_id = str(uuid.uuid4())
            minted = True
            if isinstance(request_data, dict):
                request_data['event_id'] = event_id
        try:
            session_id_log = request_data.get('session_id') if isinstance(request_data, dict) else None
            self.logger.info(
                f"stop_event dispatch request_id={event_id} session={session_id_log} "
                f"request_id_minted={'true' if minted else 'false'}"
            )
        except Exception:
            pass
        session_id = request_data.get('session_id') or 'default'
        is_shadow = bool(request_data.get('shadow', False))

        # Forced continuation: skip to prevent infinite loops. Skip even in shadow mode.
        if request_data.get('stop_hook_active'):
            return {
                'status': 'skipped',
                'event_id': event_id,
                'category': None,
                'tier': self._tier_value(session_id),
                'queued': False,
                'reason': 'stop_hook_active=true (forced continuation)',
            }

        if self._content_router is None or self._queue_manager is None:
            return {
                'status': 'error',
                'event_id': event_id,
                'category': None,
                'tier': None,
                'queued': False,
                'reason': 'pipeline not initialized (ContentRouter/QueueManager missing)',
            }

        # Route + notify QueueManager. Both run on the pipeline loop.
        try:
            result = self._run_pipeline_coroutine(
                self._content_router.route(request_data), timeout=5.0,
            )
        except Exception as e:
            self.log(f"stop_event router error: {e}")
            self._log_shadow_decision('stop_event', request_data, None, error=str(e), is_shadow=is_shadow)
            return {
                'status': 'error',
                'event_id': event_id,
                'category': None,
                'tier': self._tier_value(session_id),
                'queued': False,
                'reason': f'router error: {e}',
            }

        # Always log to shadow.log.
        self._log_shadow_decision('stop_event', request_data, result, is_shadow=is_shadow)

        # Shadow mode: classified but don't submit and don't touch QM state.
        if is_shadow:
            return {
                'status': 'shadowed',
                'event_id': result.decision.source_event_id if result else event_id,
                'category': result.decision.category.value if result else None,
                'tier': self._tier_value(session_id),
                'queued': False,
                'reason': 'shadow mode — classified but not submitted',
            }

        # Always notify QueueManager so it can flush per-session state.
        try:
            self._run_pipeline_coroutine(
                self._queue_manager.on_stop_hook(session_id), timeout=5.0,
            )
        except Exception as e:
            self.log(f"stop_event on_stop_hook failed: {e}")
            # Non-fatal — continue to submission below.

        if result is None:
            return {
                'status': 'skipped',
                'event_id': event_id,
                'category': None,
                'tier': self._tier_value(session_id),
                'queued': False,
                'reason': 'silence default',
            }

        # FINAL_ANSWER / INSIGHT — submit directly, no further batching.
        # OBSERVE-01: forward request_id for end-to-end traceability.
        ok = self._submit_to_pipeline(
            result.decision.content, session_id, result.decision.priority,
            request_id=result.decision.source_event_id or event_id,
        )
        return {
            'status': 'accepted' if ok else 'error',
            'event_id': result.decision.source_event_id or event_id,
            'category': result.decision.category.value,
            'tier': self._tier_value(session_id),
            'queued': bool(ok),
            'reason': 'submitted to pipeline' if ok else 'pipeline submit failed',
        }

    def _submit_to_pipeline(
        self,
        content: str,
        session_id: str,
        priority: int,
        request_id: Optional[str] = None,
    ) -> bool:
        """Submit a routed item directly to PipelineAdapter (skipping
        TurnBuffer). Returns True on success, False otherwise.

        OBSERVE-01: ``request_id`` is forwarded through the adapter →
        orchestrator → IngestMessage chain so logs at every pipeline
        stage share the same UUID for traceability.
        """
        if not content or self._pipeline_adapter is None:
            return False
        try:
            self._pipeline_adapter.submit_async(
                content=content, session_id=session_id, priority=priority,
                request_id=request_id,
            )
            return True
        except Exception as e:
            self.log(f"pipeline submit failed: {e}")
            return False

    def _tier_value(self, session_id: str):
        """Return the QueueManager tier value (string) for response payloads,
        or None if QueueManager is not initialized.
        """
        if self._queue_manager is None:
            return None
        try:
            tier = self._queue_manager.get_tier(session_id)
            return tier.value if tier is not None else None
        except Exception:
            return None

    def _get_last_audio_played_at(self) -> Optional[float]:
        """OBSERVE-02 — return PlaybackStage.last_audio_played_at if reachable.

        The daemon does not directly hold a PlaybackStage; it lives at
        ``self._pipeline_adapter._orchestrator.playback``. Returns ``None``
        if any link in that chain is missing (e.g. pipeline not yet booted)
        so the health endpoint stays reportable even under degraded states.
        """
        try:
            adapter = self._pipeline_adapter
            if adapter is None:
                return None
            orchestrator = getattr(adapter, '_orchestrator', None)
            if orchestrator is None:
                return None
            playback = getattr(orchestrator, 'playback', None)
            if playback is None:
                return None
            return getattr(playback, 'last_audio_played_at', None)
        except Exception:
            return None

    @staticmethod
    def _skip_excerpt(kind: str, request_data: dict) -> str:
        """Best-effort extraction of the raw content for a SKIPPED event, so
        recall (false-negatives) is measurable offline. Defensive: handles the
        dict/str shapes tool_response and stop-event content arrive in."""
        if not isinstance(request_data, dict):
            return ""
        if kind == "stop_event":
            return str(request_data.get("content") or request_data.get("response") or "")
        # tool_event: stdout/stderr live under tool_response (dict or str).
        resp = request_data.get("tool_response")
        if isinstance(resp, dict):
            return str(resp.get("stdout") or resp.get("stderr") or resp.get("output") or "")
        if isinstance(resp, (str, list)):
            return str(resp)
        return str(request_data.get("content") or "")

    def _log_shadow_decision(self, kind: str, request_data: dict, result, *,
                             error: str = "", is_shadow: bool = False) -> None:
        """Append a JSONL entry to ~/.claude/logs/tts/shadow.log per Wave 2.5.

        Captures the new pipeline's classification decision for every event
        the daemon sees. During the 24h shadow soak, hooks dual-write — the
        legacy path drives actual audio, the new (shadowed) path just lands
        decisions here for offline confusion-matrix analysis.

        Args:
            kind: 'tool_event' or 'stop_event' (matches the socket command)
            request_data: the inbound request dict (we extract a few fields)
            result: ContentRouter.route() return value, or None on drop/error
            error: optional error string (when ContentRouter raised)
            is_shadow: whether this event was sent with shadow=true (otherwise
                it represents real production traffic — still useful audit)
        """
        try:
            log_path = Path.home() / ".claude" / "logs" / "tts" / "shadow.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.time(),
                "kind": kind,
                "is_shadow": is_shadow,
                # Active judge+summarizer model — tags every decision so post-swap
                # data (e.g. qwen2.5-coder) is cleanly separable from the model
                # baseline when comparing regimes offline (Phase 3 instrumentation).
                "model": getattr(getattr(self, "_ollama_summarizer", None), "_model", None),
                "event_id": request_data.get("event_id", ""),
                "session_id": request_data.get("session_id", ""),
                "tool_name": request_data.get("tool_name") if kind == "tool_event" else None,
                "stop_hook_active": bool(request_data.get("stop_hook_active", False)) if kind == "stop_event" else None,
                "new_decision": None if result is None else {
                    "should_speak": result.decision.should_speak,
                    "category": result.decision.category.value,
                    "priority": result.decision.priority,
                    "context_hint": result.decision.context_hint,
                    "raw_excerpt": result.decision.raw_excerpt[:120],
                    "content_len": len(result.decision.content),
                    "needs_summarization": result.decision.needs_summarization,
                },
                "error": error or None,
            }
            # RECALL INSTRUMENTATION: when a SKIP discards the decision
            # (result is None, not an error), capture WHAT was skipped AND WHY
            # so false-negatives ("good content filtered out") become measurable
            # offline. The reason is recovered from router._last_drop_reason
            # (route() collapses skips to None but now records the reason there).
            if result is None and not error:
                skipped = self._skip_excerpt(kind, request_data)
                if skipped:
                    # Widened 200 -> 600 chars: the drop trigger often lives in
                    # the body, not the first 200 chars (2026-06-19 recall pass).
                    entry["skipped_excerpt"] = skipped[:600]
                    entry["skipped_len"] = len(skipped)
                # The drop REASON (drop-check / backpressure / dedup) — recovered
                # from the router, which route() otherwise discards on its None.
                router = getattr(self, "_content_router", None)
                reason = getattr(router, "_last_drop_reason", "") if router else ""
                if reason:
                    entry["skip_reason"] = reason
            with open(log_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            # Never let shadow logging break the request path.
            try:
                self.log(f"shadow log write failed: {e}")
            except Exception:
                pass

    def _load_voice_assignments(self):
        """Load persistent voice assignments from disk"""
        voice_file = _VOICE_ASSIGNMENT_FILE
        if voice_file.exists():
            try:
                with open(voice_file, 'r') as f:
                    assignments = json.load(f)
                    # Validate assignments
                    valid_assignments = {}
                    for session_id, voice_data in assignments.items():
                        if isinstance(voice_data, dict) and 'voice' in voice_data:
                            valid_assignments[session_id] = voice_data
                        elif isinstance(voice_data, str):  # Legacy format
                            valid_assignments[session_id] = {
                                'voice': voice_data,
                                'hash': self._generate_voice_hash(session_id, voice_data),
                                'assigned_at': time.time()
                            }
                    return valid_assignments
            except Exception as e:
                self.log(f"Failed to load voice assignments: {e}")
        return {}

    def _save_voice_assignments(self):
        """Save voice assignments to persistent storage"""
        try:
            voice_file = _VOICE_ASSIGNMENT_FILE
            voice_file.parent.mkdir(parents=True, exist_ok=True)
            with open(voice_file, 'w') as f:
                json.dump(self.session_voices, f, indent=2)
        except Exception as e:
            self.log(f"Failed to save voice assignments: {e}")

    def _generate_voice_hash(self, session_id: str, voice: str) -> str:
        """Generate deterministic hash for voice assignment conflict resolution"""
        # Use both session_id and voice to create a unique hash
        combined = f"{session_id}:{voice}:{int(time.time() / 86400)}"  # Daily rotation
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    def _resolve_voice_conflict(self, session_id: str, preferred_voice: str, all_voices: List[str]) -> str:
        """Resolve voice assignment conflicts using deterministic algorithm"""
        # Create a deterministic but unique selection based on session characteristics
        session_bytes = session_id.encode()

        # Generate multiple hash attempts for conflict resolution
        for attempt in range(len(all_voices)):
            # Create hash with attempt number for deterministic collision resolution
            hash_input = session_bytes + str(attempt).encode()
            hash_value = hashlib.sha256(hash_input).hexdigest()
            voice_index = int(hash_value[:8], 16) % len(all_voices)
            candidate_voice = all_voices[voice_index]

            # Check if this voice is available or has the least conflicts
            voice_sessions = [
                sid for sid, voice_data in self.session_voices.items()
                if voice_data.get('voice') == candidate_voice
            ]

            # If voice is free or has minimal conflicts, use it
            if len(voice_sessions) <= attempt // len(all_voices):
                return candidate_voice

        # Fallback to preferred voice if all else fails
        return preferred_voice

    def assign_session_voice(self, session_id: str) -> str:
        """Simplified voice assignment - always use Ava for consistency"""
        with self.voice_assignment_lock:
            # Always use Ava's voice for consistency
            # This is the slightly raspy younger female voice the user prefers
            selected_voice = 'en-US-AvaNeural'

            # Generate voice hash for this assignment
            voice_assignment_hash = self._generate_voice_hash(session_id, selected_voice)

            # Store assignment with metadata
            self.session_voices[session_id] = {
                'voice': selected_voice,
                'hash': voice_assignment_hash,
                'assigned_at': time.time(),
                'conflict_resolution': False  # No conflicts when always using same voice
            }

            self.log(f"Assigned voice {selected_voice} to session {session_id} (hash: {voice_assignment_hash[:8]})")

            # Save to persistent storage
            self._save_voice_assignments()
            self.save_state()

            return selected_voice
    
    def is_duplicate(self, content: str) -> bool:
        """Check if content was recently spoken"""
        # Clean old hashes
        current_time = time.time()
        if current_time - self.hash_cleanup_time > self.hash_expiry:
            self.recent_hashes.clear()
            self.hash_cleanup_time = current_time
        
        # Check for duplicate
        content_hash = hashlib.md5(content.lower().strip().encode()).hexdigest()[:12]
        if content_hash in self.recent_hashes:
            self.stats['duplicates_prevented'] += 1
            return True
        
        self.recent_hashes[content_hash] = time.time()
        return False
    
    def _restore_contractions_daemon(self, text: str) -> str:
        """
        Restore contractions for natural speech
        Contracts expanded forms like "I am" → "I'm", "that is" → "that's"
        """
        # Defensive check for production robustness
        if not text:
            return text or ""

        # Comprehensive contractions mapping for natural speech
        # CRITICAL: This contracts expanded forms → natural contractions
        standard_contractions = {
            # Negative contractions (highest priority)
            r"\bdo not\b": "don't",
            r"\bdoes not\b": "doesn't",
            r"\bdid not\b": "didn't",
            r"\bwill not\b": "won't",
            r"\bcannot\b": "can't",
            r"\bcan not\b": "can't",
            r"\bcould not\b": "couldn't",
            r"\bshould not\b": "shouldn't",
            r"\bwould not\b": "wouldn't",
            r"\bis not\b": "isn't",
            r"\bare not\b": "aren't",
            r"\bwas not\b": "wasn't",
            r"\bwere not\b": "weren't",
            r"\bhas not\b": "hasn't",
            r"\bhave not\b": "haven't",
            r"\bhad not\b": "hadn't",
            r"\bmust not\b": "mustn't",
            r"\bmight not\b": "mightn't",
            r"\bneed not\b": "needn't",

            # Pronoun + be contractions
            r"\bI am\b": "I'm",
            r"\byou are\b": "you're",
            r"\bhe is\b": "he's",
            r"\bshe is\b": "she's",
            r"\bit is\b": "it's",
            r"\bwe are\b": "we're",
            r"\bthey are\b": "they're",
            r"\bthat is\b": "that's",
            r"\bwho is\b": "who's",
            r"\bwhat is\b": "what's",
            r"\bthere is\b": "there's",
            r"\bhere is\b": "here's",
            r"\bwhere is\b": "where's",
            r"\bhow is\b": "how's",

            # Pronoun + have contractions
            r"\bI have\b": "I've",
            r"\byou have\b": "you've",
            r"\bwe have\b": "we've",
            r"\bthey have\b": "they've",
            r"\bwho have\b": "who've",

            # Pronoun + will contractions
            r"\bI will\b": "I'll",
            r"\byou will\b": "you'll",
            r"\bhe will\b": "he'll",
            r"\bshe will\b": "she'll",
            r"\bit will\b": "it'll",
            r"\bwe will\b": "we'll",
            r"\bthey will\b": "they'll",
            r"\bthat will\b": "that'll",
            r"\bwho will\b": "who'll",

            # Pronoun + would contractions
            r"\bI would\b": "I'd",
            r"\byou would\b": "you'd",
            r"\bhe would\b": "he'd",
            r"\bshe would\b": "she'd",
            r"\bit would\b": "it'd",
            r"\bwe would\b": "we'd",
            r"\bthey would\b": "they'd",
            r"\bwho would\b": "who'd",

            # Pronoun + had contractions (same forms as would)
            r"\bI had\b": "I'd",
            r"\byou had\b": "you'd",
            r"\bhe had\b": "he'd",
            r"\bshe had\b": "she'd",
            r"\bwe had\b": "we'd",
            r"\bthey had\b": "they'd",

            # Let us
            r"\blet us\b": "let's",
        }

        # Apply contractions (sorted by length to avoid partial replacements)
        sorted_patterns = sorted(standard_contractions.items(), key=lambda x: len(x[0]), reverse=True)

        for pattern, contraction in sorted_patterns:
            def replacement_func(match):
                matched_text = match.group(0)
                # Preserve capitalization
                if matched_text.isupper():
                    return contraction.upper()
                elif matched_text[0].isupper():
                    return contraction[0].upper() + contraction[1:]
                return contraction

            text = re.sub(pattern, replacement_func, text, flags=re.IGNORECASE)

        return text

    def clean_text_for_speech(self, text: str) -> str:
        """Clean text for natural speech"""
        # PROTECT SSML markers before cleaning - these contain prosody for inflection
        ssml_placeholder = "___SSML_PROTECTED___"
        ssml_match = re.search(r'\[\[SSML:(.*?)\]\]', text)
        ssml_content = None
        if ssml_match:
            ssml_content = ssml_match.group(0)  # Save full marker including brackets
            text = text.replace(ssml_content, ssml_placeholder)

        # CRITICAL FIX: Contract expanded forms for natural speech
        # This handles cases where Claude generates formal text like "I am" instead of "I'm"
        text = self._restore_contractions_daemon(text)

        # Remove emojis - comprehensive Unicode range coverage
        # This covers most common emoji ranges including:
        # - Emoticons (1F600-1F64F)
        # - Miscellaneous Symbols and Pictographs (1F300-1F5FF)
        # - Transport and Map Symbols (1F680-1F6FF)
        # - Regional Indicator Symbols (1F1E0-1F1FF)
        # - Supplemental Symbols and Pictographs (1F900-1F9FF)
        # - Additional emoticons (1FA70-1FAFF)
        # - Basic Latin symbols that are often used as emoji (2600-27BF)
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
            "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-A
            "\U00002600-\U000027BF"  # miscellaneous symbols & dingbats
            "\U0001F700-\U0001F77F"  # alchemical symbols
            "\U0001F780-\U0001F7FF"  # geometric shapes extended
            "\U0001F800-\U0001F8FF"  # supplemental arrows-C
            "\U00002300-\U000023FF"  # miscellaneous technical
            "\U00002B50-\U00002B55"  # stars
            "\U000025A0-\U000025FF"  # geometric shapes
            "\U00002700-\U000027BF"  # dingbats
            "\U0000FE0F"             # variation selector
            "\U00003030"             # wavy dash
            "\U000000A9"             # copyright
            "\U000000AE"             # registered
            "\U00002122"             # trademark
            "]+",
            flags=re.UNICODE
        )
        text = emoji_pattern.sub('', text)

        # Remove code blocks and technical markers
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\{[^}]+\}', '', text)

        # Clean markdown
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

        # Normalize whitespace (this will also clean up spaces left by emoji removal)
        text = ' '.join(text.split())

        # Limit length
        if len(text) > 500:
            text = text[:500] + "..."

        # RESTORE SSML markers after cleaning
        if ssml_content and ssml_placeholder in text:
            text = text.replace(ssml_placeholder, ssml_content)

        return text.strip()

    def stop_playback(self, force: bool = False) -> bool:
        """
        Stop current TTS playback immediately using event-based signaling

        Args:
            force: If True, kill process immediately without cleanup

        Returns:
            True if playback was stopped, False if nothing was playing
        """
        # Check if there's anything to stop (no lock held)
        if not self.is_playing:
            return False

        # Signal interrupt event (atomic operation, no lock needed)
        self.interrupt_event.set()

        # Terminate process if available (brief lock just for process access)
        process_to_kill = None
        with self.current_process_lock:
            process_to_kill = self.current_process

        if process_to_kill:
            try:
                if force:
                    # Force kill immediately
                    try:
                        process_to_kill.terminate()
                        process_to_kill.wait(timeout=0.05)  # Wait 50ms max
                    except subprocess.TimeoutExpired:
                        process_to_kill.kill()
                        process_to_kill.wait(timeout=0.01)
                else:
                    # Graceful termination
                    process_to_kill.terminate()
                    try:
                        process_to_kill.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        process_to_kill.kill()

                self.logger.info("Playback stopped successfully")
                return True

            except Exception as e:
                self.log_error(ErrorCategory.AUDIO_PLAYBACK, f"Error stopping playback", e)
                return False
        else:
            return False

    def _clear_playback_state(self):
        """Clear playback state after stop"""
        self.current_process = None
        self.current_text = ""
        self.current_session_id = ""
        self.playback_start_time = 0
        self.estimated_duration = 0
        self.is_playing = False

    def get_playback_state(self) -> Dict[str, Any]:
        """
        Get current playback state for visual indicator

        Returns:
            Dictionary containing:
            - is_playing: bool
            - current_text: str (text being spoken)
            - current_session_id: str
            - progress_percent: float (0-100)
            - time_remaining_ms: int (estimated milliseconds remaining)
            - elapsed_ms: int (milliseconds elapsed)
        """
        # No lock needed - atomic reads of simple types
        if not self.is_playing:
            return {
                'is_playing': False,
                'current_text': '',
                'current_session_id': '',
                'progress_percent': 0.0,
                'time_remaining_ms': 0,
                'elapsed_ms': 0
            }

        # Read state atomically (no lock - these are simple assignments)
        elapsed = time.time() - self.playback_start_time
        elapsed_ms = int(elapsed * 1000)
        current_text = self.current_text
        current_session_id = self.current_session_id
        estimated_duration = self.estimated_duration

        # Calculate progress
        if estimated_duration > 0:
            progress_percent = min(100.0, (elapsed / estimated_duration) * 100)
            time_remaining = max(0, estimated_duration - elapsed)
            time_remaining_ms = int(time_remaining * 1000)
        else:
            progress_percent = 0.0
            time_remaining_ms = 0

        return {
            'is_playing': True,
            'current_text': current_text,
            'current_session_id': current_session_id,
            'progress_percent': progress_percent,
            'time_remaining_ms': time_remaining_ms,
            'elapsed_ms': elapsed_ms
        }

    def register_state_callback(self, callback: callable):
        """
        Register callback for playback state changes

        Args:
            callback: Function to call when state changes.
                     Signature: callback(event: str, state: Dict)
                     Events: 'started', 'stopped', 'completed', 'error'
        """
        with self.state_callbacks_lock:
            if callback not in self.state_callbacks:
                self.state_callbacks.append(callback)
                self.logger.debug(f"Registered state callback: {callback.__name__ if hasattr(callback, '__name__') else 'anonymous'}")

    def unregister_state_callback(self, callback: callable):
        """Remove a registered state callback"""
        with self.state_callbacks_lock:
            if callback in self.state_callbacks:
                self.state_callbacks.remove(callback)
                self.logger.debug(f"Unregistered state callback")

    def _notify_state_change(self, event: str):
        """Notify all registered callbacks of state change"""
        state = self.get_playback_state()

        with self.state_callbacks_lock:
            callbacks = self.state_callbacks.copy()

        for callback in callbacks:
            try:
                callback(event, state)
            except Exception as e:
                self.log_error(ErrorCategory.SYSTEM, f"State callback error", e)

    # LEGACY-04: _estimate_speech_duration, _process_ssml_markers,
    # _generate_ssml_audio, speak_text, process_session_queue,
    # get_session_queue, add_request, _get_queue_position — all deleted.
    # These methods were only reachable through the 'speak' socket command
    # (deleted in LEGACY-03) and the SessionQueue class (deleted above).

    def migrate_session_to_new_daemon(self, session_id: str) -> bool:
        """Migrate session state for daemon restart recovery"""
        try:
            with self.queue_lock:
                if session_id not in self.session_queues:
                    return False

                queue = self.session_queues[session_id]
                if not queue.can_migrate():
                    self.log(f"Session {session_id} cannot be migrated (too many failures/migrations)")
                    return False

                # Prepare migration data
                migration_data = queue.prepare_for_migration()

                # Save persistent state for new daemon to pick up
                success = queue.save_persistent_state()

                if success:
                    self.log(f"Successfully prepared session {session_id} for migration")
                    return True
                else:
                    self.log(f"Failed to save migration state for session {session_id}")
                    return False

        except Exception as e:
            self.log(f"Migration preparation failed for session {session_id}: {e}")
            return False

    def cleanup_stale_sessions(self):
        """Enhanced cleanup with migration support and resource limits"""
        current_time = time.time()

        with self.queue_lock:
            # Identify different types of sessions to clean up
            stale_sessions = []
            migratable_sessions = []
            terminated_sessions = []

            for sid, queue in list(self.session_queues.items()):
                if queue.state == SessionState.TERMINATED:
                    terminated_sessions.append(sid)
                elif queue.is_stale() and queue.is_empty():
                    if queue.can_migrate():
                        migratable_sessions.append(sid)
                    else:
                        stale_sessions.append(sid)
                elif queue.error_count >= queue.resource_limits['max_errors']:
                    stale_sessions.append(sid)

            # Handle terminated sessions immediately
            for sid in terminated_sessions:
                self._cleanup_session(sid, "terminated")

            # Try to migrate viable sessions
            for sid in migratable_sessions:
                if self.migrate_session_to_new_daemon(sid):
                    self.log(f"Session {sid} prepared for migration")
                else:
                    stale_sessions.append(sid)  # Migration failed, mark for cleanup

            # Clean up truly stale sessions
            for sid in stale_sessions:
                self._cleanup_session(sid, "stale")

            # Enforce session limits
            if len(self.session_queues) > _SESSION_MAX_SESSIONS:
                excess_sessions = len(self.session_queues) - _SESSION_MAX_SESSIONS
                # Remove oldest sessions that are idle
                idle_sessions = [
                    (sid, queue) for sid, queue in self.session_queues.items()
                    if queue.state == SessionState.IDLE
                ]
                idle_sessions.sort(key=lambda x: x[1].last_activity)

                for sid, _ in idle_sessions[:excess_sessions]:
                    self._cleanup_session(sid, "resource_limit")

            total_cleaned = len(terminated_sessions) + len(stale_sessions)
            if total_cleaned > 0:
                self.log(f"Cleaned up {total_cleaned} sessions (terminated: {len(terminated_sessions)}, stale: {len(stale_sessions)})")
                self.stats['sessions_active'] = len(self.session_queues)
                self.save_state()
                self._save_voice_assignments()

    def _cleanup_session(self, session_id: str, reason: str):
        """Clean up individual session with proper resource deallocation"""
        try:
            # Remove from session queues (legacy SessionQueue.terminate() removed — LEGACY-04)
            if session_id in self.session_queues:
                del self.session_queues[session_id]

            # Clean up processing threads
            if session_id in self.processing_threads:
                thread = self.processing_threads[session_id]
                if thread.is_alive():
                    # Give thread a moment to finish current operation
                    thread.join(timeout=1.0)
                del self.processing_threads[session_id]

            # Clean up voice assignments
            with self.voice_assignment_lock:
                if session_id in self.session_voices:
                    voice_data = self.session_voices[session_id]
                    released_voice = voice_data.get('voice') if isinstance(voice_data, dict) else voice_data
                    del self.session_voices[session_id]
                    self.log(f"Released voice {released_voice} from session {session_id} (reason: {reason})")

            # Clean up persistent state files
            state_file = _SESSION_STATE_DIR / f"{session_id}.json"
            if state_file.exists():
                try:
                    state_file.unlink()
                except Exception as e:
                    self.log(f"Failed to remove state file for {session_id}: {e}")

            self.log(f"Session {session_id} cleaned up (reason: {reason})")

        except Exception as e:
            self.log(f"Error cleaning up session {session_id}: {e}")
    
    def get_stats(self) -> Dict:
        """Get daemon statistics"""
        runtime = time.time() - self.stats['daemon_start']
        return {
            **self.stats,
            'runtime_seconds': runtime,
            'active_queues': len(self.session_queues),
            'active_threads': sum(1 for t in self.processing_threads.values() if t.is_alive()),
            'voice_assignments': len(self.session_voices)
        }

    # ------------------------------------------------------------------
    # Wave 2 wiring: pipeline singletons + new socket-event endpoints.
    # ------------------------------------------------------------------

    def _load_tts_user_config(self) -> dict:
        """Best-effort load of config/tts_user_config.json.

        Returns an empty dict on any failure — ContentRouter and QueueManager
        both have safe defaults for missing keys.
        """
        # Project-relative path; daemon may be launched from anywhere so try
        # both the package dir and the home-directory copy.
        candidates = [
            config_path(),
            Path(__file__).resolve().parent.parent / "config" / "tts_user_config.json",
            Path.home() / ".claude" / "tts" / "config" / "tts_user_config.json",
        ]
        for path in candidates:
            try:
                if path.exists():
                    with open(path, "r") as f:
                        cfg = json.load(f)
                    if isinstance(cfg, dict):
                        self.log(f"Loaded TTS user config from {path}")
                        return cfg
            except Exception as e:
                self.log(f"Failed to load tts_user_config from {path}: {e}")
        return {}

    def _start_pipeline_singletons(self):
        """Instantiate Wave 2 pipeline singletons and pre-warm Ollama.

        Order matters:
            1. PipelineAdapter (owns the asyncio loop + orchestrator)
            2. OllamaClient (sync; cheap construction)
            3. OllamaSummarizer (async wrapper around the client)
            4. ContentRouter (depends on summarizer)
            5. QueueManager (depends on summarizer + ingest_stage + playback_stage)
            6. Late-bind QueueManager into ContentRouter so pressure flows.
            7. Schedule warmup() on the pipeline event loop so the first burst
               doesn't pay cold-start latency.

        Failures are logged but do not abort startup — the legacy `speak`
        path stays functional even if these singletons fail to come up.
        """
        # Wave 2 imports kept local so a missing module doesn't break the
        # daemon entirely (legacy speak path still works).
        try:
            from daemon.ollama_integration import OllamaClient
            from daemon.ollama_summarizer import OllamaSummarizer
            from daemon.content_router import ContentRouter
            from daemon.pipeline.queue_manager import QueueManager
            from daemon.pipeline.adapter import PipelineAdapter
        except Exception as e:
            self.log(f"WARN: Wave 2 pipeline imports failed; new endpoints disabled: {e}")
            return

        # Load config (best-effort — empty dict is fine).
        self._tts_user_config = self._load_tts_user_config()

        # 1) PipelineAdapter — owns the asyncio loop + orchestrator.
        try:
            voice_cfg = (self._tts_user_config.get("voice", {})
                         if isinstance(self._tts_user_config.get("voice"), dict) else {})
            voice_name = voice_cfg.get("name", "en-US-AvaNeural")
            # Engine selection (R-engine): "kokoro"/"mlx-audio" routes through a
            # persistent local MLX Kokoro worker; anything else uses edge-tts.
            engine = str(voice_cfg.get("engine", "edge-tts"))
            speed = float(voice_cfg.get("rate", 1.0))
            mlx_python = voice_cfg.get("mlx_python")  # None → KokoroEngine default
            kokoro_model = voice_cfg.get("kokoro_model")
            # Voicebox backend config (only used when engine == "voicebox").
            voicebox_config = (self._tts_user_config.get("voicebox")
                               if isinstance(self._tts_user_config.get("voicebox"), dict)
                               else None)
            # afplay -v gain. Was previously unwired (config volume ignored).
            volume = float(voice_cfg.get("volume", 1.0))
            self._pipeline_adapter = PipelineAdapter(
                voice=voice_name,
                engine=engine,
                speed=speed,
                mlx_python=mlx_python,
                kokoro_model=kokoro_model,
                voicebox_config=voicebox_config,
                volume=volume,
            )
            self._pipeline_adapter.start()
            self.log(
                f"PipelineAdapter started (engine={engine}, voice={voice_name}, "
                f"speed={speed}, volume={volume})"
            )

            # DAEMON-04/N6: install a global asyncio exception handler on the
            # pipeline event loop so unhandled task exceptions are logged rather
            # than silently discarded at garbage-collection time.
            loop = getattr(self._pipeline_adapter, "_loop", None)
            if loop is not None:
                def _asyncio_exc_handler(loop, context):
                    exc = context.get("exception")
                    msg = context.get("message", "no message")
                    if exc is not None:
                        self.log(
                            f"asyncio unhandled exception: {msg} — {type(exc).__name__}: {exc}"
                        )
                    else:
                        self.log(f"asyncio exception context: {msg}")
                loop.call_soon_threadsafe(
                    loop.set_exception_handler, _asyncio_exc_handler
                )
                self.log("asyncio global exception handler installed (DAEMON-04)")
        except Exception as e:
            self.log(f"ERROR: PipelineAdapter start failed: {e}")
            self._pipeline_adapter = None
            return  # Without the adapter the rest is moot.

        # 2-3) Ollama client + summarizer.
        try:
            self._ollama_client = OllamaClient()
            # R2: config-driven summarizer budget + warmth. Defaults embedded so
            # a missing/partial config still works. inner_timeout_s raised to 2.0
            # (live `model` takes ~2s on long content); keep_alive holds the
            # model resident so it doesn't go cold between bursts.
            _summ_cfg = (self._tts_user_config or {}).get("summarizer", {})
            self._ollama_summarizer = OllamaSummarizer(
                self._ollama_client,
                model=str(_summ_cfg.get("model", "qwen2.5-coder:1.5b")),
                timeout_s=float(_summ_cfg.get("inner_timeout_s", 5.0)),
                keep_alive=_summ_cfg.get("keep_alive", "30m"),
                warm_interval_s=float(_summ_cfg.get("warm_interval_s", 120.0)),
                soft_tokens=int(_summ_cfg.get("soft_tokens", 200)),
                slack_tokens=int(_summ_cfg.get("slack_tokens", 96)),
            )
            self.log(
                f"OllamaClient + OllamaSummarizer ready "
                f"(inner_timeout={self._ollama_summarizer._timeout_s}s, "
                f"keep_alive={self._ollama_summarizer._keep_alive})"
            )
        except Exception as e:
            self.log(f"WARN: Ollama init failed; summaries will use fallback: {e}")
            self._ollama_client = None
            self._ollama_summarizer = None

        # 4) LLM provider — wraps the summarizer (or NullProvider if Ollama is down).
        from daemon.providers import make_provider
        self._llm_provider = make_provider(self._tts_user_config, self._ollama_summarizer)

        # 5) ContentRouter — depends on the provider.
        try:
            self._content_router = ContentRouter(
                config=self._tts_user_config,
                provider=self._llm_provider,
            )
            self.log("ContentRouter ready")
        except Exception as e:
            self.log(f"ERROR: ContentRouter init failed: {e}")
            self._content_router = None

        # 5) QueueManager — needs the orchestrator's ingest + playback stages.
        try:
            orchestrator = getattr(self._pipeline_adapter, "_orchestrator", None)
            if orchestrator is None:
                raise RuntimeError("PipelineAdapter orchestrator is None (start() did not complete)")
            self._queue_manager = QueueManager(
                config=self._tts_user_config,
                ollama_summarizer=self._ollama_summarizer,
                ingest_stage=orchestrator.ingest,
                playback_stage=orchestrator.playback,
            )
            self.log("QueueManager ready")
        except Exception as e:
            self.log(f"ERROR: QueueManager init failed: {e}")
            self._queue_manager = None

        # 6) Late-bind QueueManager into ContentRouter so backpressure flows.
        if self._content_router is not None and self._queue_manager is not None:
            try:
                self._content_router.set_queue_manager(self._queue_manager)
                self.log("ContentRouter ↔ QueueManager wired")
            except Exception as e:
                self.log(f"WARN: set_queue_manager failed: {e}")

        # 6.5) Wire TurnBuffer flush callback so INSIGHT/STATUS batches reach
        #      the pipeline. Without this, ContentRouter.turn_buffer_for()
        #      raises RuntimeError on the first INSIGHT/STATUS event.
        #      The callback is async (TurnBuffer awaits it) and runs in the
        #      pipeline loop; submit each RoutedItem via the orchestrator
        #      directly (already async — no thread bridging needed).
        orchestrator = (
            getattr(self._pipeline_adapter, "_orchestrator", None)
            if self._pipeline_adapter is not None else None
        )
        if (self._content_router is not None
                and orchestrator is not None
                and hasattr(orchestrator, "submit")):
            async def _turn_buffer_flush(items):
                """Per-batch flush: submit every RoutedItem to the pipeline.
                Items in a batch share session_id; submission order preserved."""
                for item in items:
                    try:
                        await orchestrator.submit(
                            item.decision.content,
                            item.session_id,
                            item.decision.priority,
                        )
                    except Exception as e:
                        # Log per-item failures but keep draining; one bad item
                        # shouldn't drop the rest of the batch.
                        try:
                            self.log(f"WARN: TurnBuffer submit failed: {e}")
                        except Exception:
                            pass

            try:
                self._content_router.set_turn_buffer_callback(_turn_buffer_flush)
                self.log("ContentRouter ↔ TurnBuffer flush callback wired")
            except Exception as e:
                self.log(f"WARN: set_turn_buffer_callback failed: {e}")
        else:
            self.log(
                f"WARN: TurnBuffer callback NOT wired "
                f"(content_router={self._content_router is not None}, "
                f"orchestrator={orchestrator is not None})"
            )

        # 7) Schedule the Ollama keep-warm LOOP on the pipeline loop (R2).
        #    A one-shot warmup is insufficient: Ollama unloads the model after
        #    its keep_alive window, so the model goes cold during idle gaps and
        #    the next burst pays cold-load latency -> timeout -> fallback. The
        #    loop re-warms periodically; combined with keep_alive on every real
        #    call the model stays resident. Fire-and-forget; never raises.
        if (self._ollama_summarizer is not None
                and self._pipeline_adapter is not None
                and getattr(self._pipeline_adapter, "_loop", None) is not None):
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ollama_summarizer.keep_warm_loop(),
                    self._pipeline_adapter._loop,
                )
                self.log("Scheduled Ollama keep-warm loop on pipeline loop")
            except Exception as e:
                self.log(f"WARN: keep-warm schedule failed: {e}")

    def _run_pipeline_coroutine(self, coro, timeout: float = 10.0):
        """Submit `coro` to the pipeline event loop and block for the result.

        Used by handle_client() (sync) to call ContentRouter / QueueManager
        async methods. Returns the coroutine's return value, or raises
        the exception it raised. Returns None if the pipeline isn't ready.
        """
        if (self._pipeline_adapter is None
                or getattr(self._pipeline_adapter, "_loop", None) is None):
            raise RuntimeError("pipeline not running")
        future = asyncio.run_coroutine_threadsafe(
            coro, self._pipeline_adapter._loop,
        )
        return future.result(timeout=timeout)

    def run(self):
        """Enhanced main daemon loop with session management"""
        # Singleton enforcement (Wave 1 W1.A): refuse to start if another
        # daemon process is already alive. Without this, two daemons could
        # contend for /tmp/tts_daemon.sock and produce non-deterministic
        # responses + double-played utterances. See plan "Risk Register".
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PID_FILE.exists():
            try:
                existing_pid = int(PID_FILE.read_text().strip())
            except (ValueError, OSError):
                existing_pid = 0
            if existing_pid > 0 and existing_pid != os.getpid():
                try:
                    os.kill(existing_pid, 0)
                    # Process exists AND it is liveness-positive. But a bare
                    # liveness check is not sufficient for a singleton guard:
                    # after a reboot the kernel readily *reuses* PID numbers,
                    # so the recorded PID may now belong to an unrelated
                    # process (observed 2026-06-16: PID 1111 reused by
                    # WavesLocalServer, which crash-looped the daemon 3147x).
                    # Verify *identity* — the PID must actually be a
                    # tts_daemon.py process — before refusing to start.
                    if _pid_is_tts_daemon(existing_pid):
                        # Genuine sibling daemon — refuse to start a second.
                        self.log(
                            f"ERROR: TTS daemon already running (PID {existing_pid}); "
                            f"refusing to start a second instance. Stop the old one "
                            f"first (kill {existing_pid}) or remove {PID_FILE} if it "
                            f"is stale."
                        )
                        print(
                            f"tts_daemon: refusing to start — PID {existing_pid} "
                            f"already alive (see {PID_FILE})",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    else:
                        # PID is alive but is NOT our daemon (PID reuse).
                        # The PID file is stale; safe to overwrite below.
                        self.log(
                            f"Removing stale PID file (PID {existing_pid} is alive "
                            f"but is not a tts_daemon process — PID reuse)"
                        )
                except ProcessLookupError:
                    # Stale PID file; safe to overwrite.
                    self.log(
                        f"Removing stale PID file (PID {existing_pid} not running)"
                    )
                except PermissionError:
                    # Process exists but we can't signal it. Confirm identity
                    # before deferring; a foreign-owned reused PID must not
                    # block us. If we cannot determine identity, err on the
                    # safe side and treat it as a live daemon.
                    if _pid_is_tts_daemon(existing_pid):
                        self.log(
                            f"ERROR: PID {existing_pid} exists but is owned by another "
                            f"user; refusing to start."
                        )
                        sys.exit(1)
                    else:
                        self.log(
                            f"Removing stale PID file (PID {existing_pid} owned by "
                            f"another user and not a tts_daemon process — PID reuse)"
                        )

        # Write PID file
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))

        # Register cleanup
        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        # OBSERVE-03: schema self-check at startup. Catches drift in
        # daemon/tts_types.py before the socket server starts accepting
        # traffic. Fails loud — better to refuse to start than to ship
        # broken validation in production.
        try:
            from daemon.schema_validator import validate_schemas_at_startup
            validate_schemas_at_startup()
            self.log("schema_validator self-check passed")
        except Exception as e:
            self.log(f"FATAL: schema_validator self-check failed: {e}")
            print(
                f"tts_daemon: FATAL — schema validation broken: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Start socket server
        self.start_socket_server()

        # Wave 2 wiring: instantiate pipeline singletons (PipelineAdapter,
        # OllamaSummarizer, ContentRouter, QueueManager) and pre-warm Ollama.
        # New socket endpoints (`tool_event` / `stop_event`) route through these.
        # Legacy `speak` continues to work even if this fails.
        self._start_pipeline_singletons()

        # Initialize session management
        last_cleanup = time.time()
        last_state_save = time.time()
        last_session_save = time.time()

        # Main loop with enhanced session management
        self.log("Enhanced TTS Daemon running with session management...")
        while not self.stop_signal.is_set():
            current_time = time.time()

            # Session cleanup every 30 seconds
            if current_time - last_cleanup > _SESSION_CLEANUP_INTERVAL:
                try:
                    self.cleanup_stale_sessions()
                    last_cleanup = current_time
                except Exception as e:
                    self.log(f"Error during session cleanup: {e}")

            # State save every 60 seconds
            if current_time - last_state_save > _SESSION_STATE_SAVE_INTERVAL:
                try:
                    self.save_state()
                    self._save_voice_assignments()
                    last_state_save = current_time
                except Exception as e:
                    self.log(f"Error saving state: {e}")

            # Save individual session states every 30 seconds
            if current_time - last_session_save > 30:
                try:
                    self._save_all_session_states()
                    last_session_save = current_time
                except Exception as e:
                    self.log(f"Error saving session states: {e}")

            # Heartbeat for active sessions
            self._update_session_heartbeats()

            # Short sleep to prevent busy waiting
            time.sleep(1)

        self.log("Enhanced TTS Daemon shutting down...")
        self.cleanup()

    def _save_all_session_states(self):
        """No-op: legacy SessionQueue removed (LEGACY-04)."""
        pass

    def _update_session_heartbeats(self):
        """No-op: legacy SessionQueue removed (LEGACY-04)."""
        pass
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals.

        DAEMON-05: on SIGTERM, cascade termination to all in-flight afplay and
        edge-tts subprocesses tracked by the pipeline before setting stop_signal.
        This prevents orphaned audio processes on daemon exit (PITFALLS N5).
        """
        self.log(f"Received signal {signum}")

        # DAEMON-05: propagate to pipeline subprocesses before stopping
        if self._pipeline_adapter is not None:
            try:
                playback = getattr(
                    self._pipeline_adapter, '_playback_stage', None
                ) or getattr(
                    getattr(self._pipeline_adapter, '_orchestrator', None),
                    'playback', None,
                )
                if playback is not None:
                    for session_id, state in playback.session_states.items():
                        proc = getattr(state, 'current_proc', None)
                        if proc is not None and proc.returncode is None:
                            try:
                                proc.terminate()
                                self.log(
                                    f"SIGTERM cascade → afplay pid={getattr(proc, 'pid', '?')} "
                                    f"(session {session_id})"
                                )
                            except Exception:
                                pass
            except Exception as e:
                self.log(f"SIGTERM cascade error: {e}")

        self.stop_signal.set()
    
    def cleanup(self):
        """Clean up resources"""
        self.save_state()

        # Wave 2: stop the pipeline gracefully so the orchestrator's
        # async loop drains in-flight work before the process exits.
        if self._pipeline_adapter is not None:
            try:
                self._pipeline_adapter.stop()
                self.log("PipelineAdapter stopped")
            except Exception as e:
                self.log(f"PipelineAdapter stop error: {e}")
            self._pipeline_adapter = None

        # Close socket
        if self.server_socket:
            self.server_socket.close()

        # Remove socket file
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        # Remove PID file
        if PID_FILE.exists():
            PID_FILE.unlink()

        # Clean up stale background processes (restart scripts, orphaned edge-tts)
        self._cleanup_background_processes()

        self.log(f"Final stats: {self.get_stats()}")
        self.log("TTS Daemon stopped")

    def _cleanup_background_processes(self):
        """Clean up stale background processes that may have been left over"""
        import subprocess
        try:
            # Kill orphaned edge-tts processes from this daemon
            subprocess.run(
                ["pkill", "-f", "edge-tts.*--write-media.*/tmp/tmp"],
                capture_output=True, timeout=5
            )
            self.log("Cleaned up orphaned edge-tts processes")
        except Exception as e:
            self.log(f"Background process cleanup (edge-tts): {e}")

        try:
            # Clean up stale audio playback processes
            subprocess.run(
                ["pkill", "-f", "afplay.*/tmp/tmp.*\\.mp3"],
                capture_output=True, timeout=5
            )
            self.log("Cleaned up orphaned audio playback processes")
        except Exception as e:
            self.log(f"Background process cleanup (afplay): {e}")


def main():
    """Main entry point"""
    daemon = TTSDaemon()
    daemon.run()


if __name__ == "__main__":
    main()