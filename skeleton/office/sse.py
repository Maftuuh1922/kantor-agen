"""Server-Sent Events stream for the browser (Req 2.8).

``event_stream`` yields SSE text frames (``event: <type>\\ndata: <json>\\n\\n``):

1. one ``snapshot`` event with the tracker's current state of every agent,
2. then live ``visual_state``, ``activity`` and ``approval_changed`` events from the bus
   (the last one tells the browser to reload the owner's approval queue),
3. plus a ``: keepalive`` comment whenever nothing was sent for
   ``keepalive_seconds`` (default 15).

The subscription is taken before the snapshot is read, so no change falls
between the two. A change that lands in both is sent twice, which is harmless
because every ``visual_state`` event carries the full state of one agent.
The subscription is closed in a ``finally`` block when the client goes away.

This module only provides the stream and the response helper. The route
``GET /api/events`` is registered by the API app.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from skeleton.core.events import EventBus, SubscriptionClosed
from skeleton.office.visual import EVENT_VISUAL_STATE, VisualTracker
from skeleton.policy.approvals import EVENT_APPROVAL_CHANGED

KEEPALIVE_SECONDS = 15.0
KEEPALIVE_FRAME = ": keepalive\n\n"
EVENT_SNAPSHOT = "snapshot"
EVENT_ACTIVITY = "activity"
FORWARDED_EVENTS = frozenset({EVENT_VISUAL_STATE, EVENT_ACTIVITY, EVENT_APPROVAL_CHANGED})
MEDIA_TYPE = "text/event-stream"


def format_sse(event: str, data: Any) -> str:
    """One SSE frame. The JSON has no raw newline, so one ``data:`` line is enough."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def event_stream(
    bus: EventBus,
    tracker: VisualTracker,
    request: Request | None = None,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    sub = bus.subscribe()
    try:
        yield format_sse(EVENT_SNAPSHOT, {"agents": tracker.current()})

        loop = asyncio.get_running_loop()
        deadline = loop.time() + keepalive_seconds
        while True:
            if request is not None and await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(sub.get(), max(0.0, deadline - loop.time()))
            except TimeoutError:
                yield KEEPALIVE_FRAME
                deadline = loop.time() + keepalive_seconds
                continue
            except SubscriptionClosed:
                return
            event_type = event.get("type")
            if event_type in FORWARDED_EVENTS:
                yield format_sse(event_type, event)
                deadline = loop.time() + keepalive_seconds
    finally:
        sub.close()


def sse_response(
    bus: EventBus,
    tracker: VisualTracker,
    request: Request | None = None,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> StreamingResponse:
    """Wrap ``event_stream`` in a streaming response for ``GET /api/events``."""
    return StreamingResponse(
        event_stream(bus, tracker, request, keepalive_seconds),
        media_type=MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
