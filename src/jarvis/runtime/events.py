"""📡 Runtime event bus.

Carries state changes and finished turns from the voice path to whoever is
watching, which today means the control centre's event stream. Publishing
never blocks and never raises: a slow or vanished watcher must not be able
to stall the assistant mid-reply.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Iterator


# A watcher that falls this far behind is not watching in any useful sense.
# Dropping its oldest events keeps publishing bounded in time and memory.
SUBSCRIBER_QUEUE_SIZE = 256


class Subscription:
    """One watcher's view of the bus."""

    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._queue: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)

    def offer(self, event: dict) -> None:
        """Hand an event over, discarding the oldest when the queue is full."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

    def listen(self, timeout: float = 15.0) -> Iterator[dict | None]:
        """Yield events as they arrive, and ``None`` when the wait lapses.

        The ``None`` lets a caller send a keep-alive so an idle connection is
        not dropped by an intermediary.
        """
        while True:
            try:
                yield self._queue.get(timeout=timeout)
            except queue.Empty:
                yield None

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class EventBus:
    """Fan-out of runtime events to every current subscriber."""

    def __init__(self) -> None:
        self._subscribers: list[Subscription] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            try:
                self._subscribers.remove(subscription)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, kind: str, data: dict | None = None) -> None:
        """Offer an event to every subscriber. Never blocks, never raises."""
        with self._lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return
        event = {"kind": kind, "data": data or {}}
        for subscription in subscribers:
            subscription.offer(event)


_bus = EventBus()


def get_event_bus() -> EventBus:
    """The process-wide bus. One daemon, one bus."""
    return _bus
