"""
Ingest Stage - Event-driven message ingest with zero-polling latency.

Replaces polling-based queue checks (100ms sleep) with asyncio Event notifications,
reducing message latency from ~105ms to <1ms.
"""

import asyncio
from asyncio import Queue, Event
from dataclasses import dataclass, field
from typing import Optional
import time
import logging

logger = logging.getLogger(__name__)


@dataclass
class IngestMessage:
    """Immutable message container for pipeline processing."""
    content: str
    session_id: str
    priority: int
    request_id: str
    ingested_at: float
    source: str = "hook"

    def __post_init__(self):
        # Ensure ingested_at is set
        if not self.ingested_at:
            self.ingested_at = time.time()


class IngestStage:
    """
    Event-driven message ingest with zero-polling latency.

    Key improvements over polling-based approach:
    - Uses asyncio.Event for immediate notification
    - Per-session event tracking for targeted wakeups
    - Backpressure handling via queue size limits
    """

    def __init__(self, max_queue_size: int = 1000):
        self.max_queue_size = max_queue_size
        self.ingest_queue: Queue[IngestMessage] = Queue(maxsize=max_queue_size)
        self.session_events: dict[str, Event] = {}
        self.session_queues: dict[str, Queue[IngestMessage]] = {}
        # Per-session lock to serialize peek_all against consume()/ingest().
        # Lazily created on first access. Held during the drain-and-refill so
        # callers can safely inspect pending items without losing or reordering.
        self._peek_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        self._stats = {
            'messages_ingested': 0,
            'messages_consumed': 0,
            'backpressure_events': 0,
        }

    async def start(self):
        """Start the ingest stage."""
        self._running = True
        logger.info("IngestStage started")

    async def stop(self):
        """Stop the ingest stage."""
        self._running = False
        # Wake up all waiting consumers
        for event in self.session_events.values():
            event.set()
        logger.info("IngestStage stopped")

    async def ingest(self, message: IngestMessage) -> bool:
        """
        Ingest a message and immediately notify waiting consumers.

        Zero-latency notification via asyncio.Event - no polling required.

        Args:
            message: The message to ingest

        Returns:
            True if ingested successfully, False if backpressure applied
        """
        session_id = message.session_id

        try:
            # Get or create session queue
            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue(maxsize=100)
                self.session_events[session_id] = Event()

            # Non-blocking put with backpressure
            self.session_queues[session_id].put_nowait(message)
            self._stats['messages_ingested'] += 1

            # Immediately wake up session consumer (zero latency!)
            self.session_events[session_id].set()

            logger.debug(
                f"Ingested message {message.request_id} for session {session_id}"
            )
            return True

        except asyncio.QueueFull:
            self._stats['backpressure_events'] += 1
            logger.warning(
                f"Backpressure: Queue full for session {session_id}"
            )
            return False

    async def consume(
        self,
        session_id: str,
        timeout: Optional[float] = None
    ) -> Optional[IngestMessage]:
        """
        Consume next message for session.

        Uses event-driven wakeup instead of polling - waits efficiently
        until a message arrives or timeout occurs.

        Args:
            session_id: The session to consume messages for
            timeout: Optional timeout in seconds

        Returns:
            The next message, or None if timeout/empty
        """
        # Get or create session resources
        if session_id not in self.session_events:
            self.session_events[session_id] = Event()
            self.session_queues[session_id] = Queue(maxsize=100)

        event = self.session_events[session_id]
        queue = self.session_queues[session_id]
        lock = self._peek_locks.setdefault(session_id, asyncio.Lock())

        # Check if message already available
        if not queue.empty():
            async with lock:
                try:
                    message = queue.get_nowait()
                    self._stats['messages_consumed'] += 1
                    return message
                except asyncio.QueueEmpty:
                    pass

        # Wait for message (no polling!)
        try:
            if timeout:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
        except asyncio.TimeoutError:
            return None
        finally:
            event.clear()

        # Retrieve message
        async with lock:
            try:
                message = queue.get_nowait()
                self._stats['messages_consumed'] += 1
                return message
            except asyncio.QueueEmpty:
                return None

    def get_stats(self) -> dict:
        """Get ingest statistics."""
        return {
            **self._stats,
            'active_sessions': len(self.session_queues),
            'pending_messages': sum(
                q.qsize() for q in self.session_queues.values()
            ),
        }

    async def cleanup_session(self, session_id: str):
        """Clean up resources for a session."""
        if session_id in self.session_events:
            self.session_events[session_id].set()  # Wake any waiters
            del self.session_events[session_id]
        if session_id in self.session_queues:
            del self.session_queues[session_id]
        if session_id in self._peek_locks:
            del self._peek_locks[session_id]
        logger.debug(f"Cleaned up ingest resources for session {session_id}")

    async def peek_all(self, session_id: str) -> list[IngestMessage]:
        """Snapshot of pending messages for a session WITHOUT consuming.

        asyncio.Queue has no peek operation, so we drain to a list under a
        per-session lock and refill in order. Holding the lock prevents
        concurrent consume()/cleanup_session() from racing.

        This is async because it acquires an asyncio.Lock that is also held
        by consume(); a sync API could not safely cooperate with the existing
        async consumer path.

        Args:
            session_id: The session to inspect

        Returns:
            Ordered list of pending messages, head-first. Empty list if no
            session or no pending messages.
        """
        queue = self.session_queues.get(session_id)
        if queue is None:
            return []

        # Lazily create per-session lock; setdefault is atomic for dict.
        lock = self._peek_locks.setdefault(session_id, asyncio.Lock())

        items: list[IngestMessage] = []
        async with lock:
            # Drain in order
            while True:
                try:
                    items.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            # Refill in original order so the next consume() sees the same
            # head-of-line item that peek reported first.
            for item in items:
                # The queue was just drained; put_nowait cannot fail unless
                # max_queue_size shrank, which never happens in practice.
                queue.put_nowait(item)

        return items

    def get_pending_count(self, session_id: str) -> int:
        """Cheap O(1) count of pending messages — no inspection.

        Returns 0 if the session has no queue yet (never had a message
        ingested or consumed).
        """
        queue = self.session_queues.get(session_id)
        if queue is None:
            return 0
        return queue.qsize()
