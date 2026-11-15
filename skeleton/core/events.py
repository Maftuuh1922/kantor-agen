"""In-process async event bus. Backs the SSE stream.

Events are plain dicts with a required `type` key. `publish` never blocks: each
subscriber has a bounded buffer, and when it is full the oldest event for that
subscriber is dropped. Subscribers only receive events published after they
subscribe.

`publish` must be called from the thread that runs the event loop (or from
plain sync code when no loop is running). It is not thread-safe.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

DEFAULT_MAXSIZE = 100


class SubscriptionClosed(Exception):
    """Raised by `Subscription.get` once the subscription is closed."""


class Subscription:
    def __init__(self, bus: EventBus, maxsize: int) -> None:
        self._bus = bus
        self._maxsize = maxsize
        self._buffer: deque[dict[str, Any]] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _push(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        if len(self._buffer) >= self._maxsize:
            self._buffer.popleft()  # slow subscriber: drop the oldest
        self._buffer.append(event)
        self._wakeup.set()

    async def get(self) -> dict[str, Any]:
        """Wait for the next event. Raises SubscriptionClosed after close()."""
        while True:
            if self._closed:
                raise SubscriptionClosed()
            if self._buffer:
                return self._buffer.popleft()
            self._wakeup.clear()
            await self._wakeup.wait()

    def close(self) -> None:
        """Stop delivery, discard buffered events, and wake any waiting get()."""
        if self._closed:
            return
        self._closed = True
        self._buffer.clear()
        self._bus.unsubscribe(self)
        self._wakeup.set()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []

    def subscribe(self, maxsize: int = DEFAULT_MAXSIZE) -> Subscription:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        sub = Subscription(self, maxsize)
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription and close it. Safe to call more than once."""
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        subscription.close()

    def publish(self, event: dict[str, Any]) -> None:
        """Deliver `event` to every current subscriber without blocking."""
        if not isinstance(event, dict) or "type" not in event:
            raise ValueError("event must be a dict with a 'type' key")
        # Copy the list: close() during delivery must not skip subscribers.
        for sub in list(self._subscriptions):
            sub._push(dict(event))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)
