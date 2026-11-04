"""In-memory, per-workflow event channels bridging worker threads and asyncio subscribers.

Producer: the execution graph, running in a worker thread, calls ``EventChannel.publish``.
Consumers: SSE handlers on the event loop, each with its own ``asyncio.Queue``.

* Thread/async safety: publish appends under a ``threading.Lock`` and hands the event to
  each subscriber's loop with ``loop.call_soon_threadsafe`` - the only thread-safe way to
  touch an asyncio queue from another thread.
* Ordering: publishes are serialised by the lock and call_soon_threadsafe callbacks run in
  FIFO order, so every subscriber sees events in event_id order.
* No gaps/duplicates on subscribe: the backlog snapshot and subscriber registration happen
  under the same lock as publish.
* Replay: the full (bounded) history is kept, so late subscribers and reconnects
  (Last-Event-ID) get everything after the id they last saw.
* Retention: a channel is kept ``retention_seconds`` after its terminal event, then evicted.

Limitations (documented in README): single process only, lost on restart, bounded history.
"""

import asyncio
import hmac
import itertools
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.observability.sanitize import sanitize
from app.schemas.events import ExecutionEvent, ExecutionEventType

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_WORKFLOW = 2_000


class ChannelBusy(Exception):
    """A stream for this workflow_id is still active."""


class BrokerFull(Exception):
    """Too many active channels."""


@dataclass
class Subscription:
    id: int
    queue: asyncio.Queue
    backlog: list[ExecutionEvent]


class EventChannel:
    def __init__(self, workflow_id: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.workflow_id = workflow_id
        self.token = secrets.token_urlsafe(32)
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[ExecutionEvent] = []
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._ids = itertools.count(1)
        self.terminal = False
        self.finished_at: float | None = None
        self.dropped = 0

    # ----- producer side (any thread) -----

    def publish(
        self,
        event_type: ExecutionEventType,
        *,
        step_number: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionEvent | None:
        with self._lock:
            if self.terminal:
                return None  # nothing may follow the terminal event
            is_terminal = event_type in (ExecutionEventType.WORKFLOW_COMPLETED, ExecutionEventType.WORKFLOW_FAILED)
            if len(self._events) >= MAX_EVENTS_PER_WORKFLOW - 1 and not is_terminal:
                self.dropped += 1  # always keep room for the terminal event
                return None
            event = ExecutionEvent(
                event_id=len(self._events) + 1,
                workflow_id=self.workflow_id,
                event_type=event_type,
                step_number=step_number,
                timestamp=datetime.now(timezone.utc),
                metadata=sanitize(metadata or {}),
            )
            self._events.append(event)
            if is_terminal:
                self.terminal = True
                self.finished_at = self._clock()
            for sub_id, (loop, queue) in list(self._subscribers.items()):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:  # subscriber's loop is closed: it is gone
                    self._subscribers.pop(sub_id, None)
            return event

    # ----- consumer side (event loop) -----

    def subscribe(self, after_event_id: int = 0) -> Subscription:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            sub_id = next(self._ids)
            backlog = [e for e in self._events if e.event_id > after_event_id]
            if not self.terminal:
                self._subscribers[sub_id] = (loop, queue)
        return Subscription(sub_id, queue, backlog)

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def events(self) -> list[ExecutionEvent]:
        with self._lock:
            return list(self._events)

    def has_events_after(self, event_id: int) -> bool:
        with self._lock:
            return bool(self._events) and self._events[-1].event_id > event_id


class EventBroker:
    def __init__(
        self,
        *,
        retention_seconds: float = 300.0,
        max_channels: int = 1_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention = retention_seconds
        self._max_channels = max_channels
        self._clock = clock
        self._lock = threading.Lock()
        self._channels: dict[str, EventChannel] = {}

    def create(self, workflow_id: str) -> EventChannel:
        with self._lock:
            self._evict_expired()
            existing = self._channels.get(workflow_id)
            if existing is not None and not existing.terminal:
                raise ChannelBusy(workflow_id)
            if existing is None and len(self._channels) >= self._max_channels:
                raise BrokerFull()
            channel = EventChannel(workflow_id, clock=self._clock)
            self._channels[workflow_id] = channel  # replaces a finished channel, if any
            return channel

    def get(self, workflow_id: str, token: str | None) -> EventChannel | None:
        """Channel for workflow_id if ``token`` matches. Unknown id and wrong token are
        indistinguishable to the caller (both None)."""
        with self._lock:
            self._evict_expired()
            channel = self._channels.get(workflow_id)
        if channel is None or not token:
            return None
        return channel if hmac.compare_digest(channel.token.encode(), token.encode()) else None

    def _evict_expired(self) -> None:
        now = self._clock()
        for wf, ch in list(self._channels.items()):
            if ch.terminal and ch.finished_at is not None and now - ch.finished_at >= self._retention:
                del self._channels[wf]

    def __len__(self) -> int:
        with self._lock:
            return len(self._channels)
