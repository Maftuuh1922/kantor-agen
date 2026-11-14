"""Visual state of each agent, decided by the backend only (Req 2.8).

``visual_state`` is a pure function. Rules, in priority order:

1. The agent or the whole business is paused -> ``paused``.
2. The agent has a running task -> ``working``, unless a handoff is pending,
   then ``handoff``.
3. The agent waits for the owner -> ``waiting``.
4. Otherwise ``idle``.

So ``working`` is returned if and only if there is a running task, neither the
agent nor the business is paused, and no handoff is pending (Property 7). A
paused agent with a running task is ``paused``, never ``working``.

``snapshot`` builds the current state of every agent of one business from the
database (pause flags), ``heartbeat.running`` and, when given, a
``VisualTracker`` (latest ``agent_state`` and ``handoff`` information).
The tracker itself works from events in order and only reads
``heartbeat.running`` once, to learn about tasks that started before it did.

``VisualTracker`` listens on the event bus and publishes one normalized
``{"type": "visual_state", agent_id, state, target_agent_id, bubble_text}``
event whenever an agent's state changes. It reacts to:

* ``agent_state`` (from the heartbeat): ``working``, ``idle`` or ``waiting``.
  It also ends a pending handoff of that agent.
* ``handoff`` (from the Manager): ``from_agent_id`` shows a handoff to
  ``to_agent_id`` for ``HANDOFF_SECONDS`` or until its next ``agent_state``.
* ``pause_changed``: the pause flags were changed (see ``controls.py``). The
  code that changes a flag must publish this event or call ``refresh()``,
  because ``controls.py`` itself does not publish.

Pause flags are always read from the database, so they cannot go stale.
Time comes from the injected clock.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from skeleton.core.clock import Clock
from skeleton.core.events import EventBus, Subscription, SubscriptionClosed

if TYPE_CHECKING:  # avoid importing the runtime just for a type
    from skeleton.runtime.heartbeat import Heartbeat

logger = logging.getLogger(__name__)

HANDOFF_SECONDS = 8.0
EXPIRY_POLL_SECONDS = 1.0
TRACKER_BUFFER = 1000

EVENT_AGENT_STATE = "agent_state"
EVENT_HANDOFF = "handoff"
EVENT_PAUSE_CHANGED = "pause_changed"
EVENT_VISUAL_STATE = "visual_state"

_REPORTABLE_STATES = frozenset({"working", "idle", "waiting"})


class VisualState(str, Enum):
    WORKING = "working"  # task running: sit at desk, type
    HANDOFF = "handoff"  # walk to the target agent's desk, talk
    WAITING = "waiting"  # stand at desk, raised hand, amber marker
    IDLE = "idle"  # the browser picks an idle behavior
    PAUSED = "paused"  # stand still at desk, grey marker


def visual_state(
    agent_paused: bool,
    business_paused: bool,
    running_task: Any,
    pending_handoff: Any,
    waiting_for_owner: bool,
) -> VisualState:
    """Decide what an agent looks like. Pure: no I/O, no clock."""
    if agent_paused or business_paused:
        return VisualState.PAUSED
    if running_task is not None:
        return VisualState.HANDOFF if pending_handoff is not None else VisualState.WORKING
    if waiting_for_owner:
        return VisualState.WAITING
    return VisualState.IDLE


# ---------------------------------------------------------------------- #
# Snapshot
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentContext:
    """What the tracker knows about one agent beyond the database."""

    reported_state: str | None = None  # last agent_state: working, idle or waiting
    bubble_text: str = ""  # bubble of that agent_state event
    handoff_target: int | None = None  # None when no handoff is pending
    handoff_text: str = ""


_NO_CONTEXT = AgentContext()
# Stands in for "a task is running" when only events are known (no heartbeat).
_EVENT_RUNNING = object()


def _load_office(conn: sqlite3.Connection, business_id: int) -> tuple[bool, list[tuple[int, str, bool]]]:
    """Return (business_paused, [(agent_id, name, paused), ...]) in agent order."""
    row = conn.execute("SELECT paused FROM business WHERE id = ?", (business_id,)).fetchone()
    # An unknown business counts as paused (fail safe, same as the heartbeat).
    business_paused = row is None or bool(row[0])
    rows = conn.execute(
        "SELECT id, name, paused FROM agent WHERE business_id = ? ORDER BY id", (business_id,)
    ).fetchall()
    return business_paused, [(int(r[0]), str(r[1]), bool(r[2])) for r in rows]


def snapshot(
    conn: sqlite3.Connection,
    business_id: int,
    heartbeat: Heartbeat | None,
    clock: Clock,
    tracker: VisualTracker | None = None,
) -> list[dict]:
    """Current visual state of every agent, ordered by agent id.

    Each item is ``{agent_id, state, target_agent_id, bubble_text}`` where
    ``state`` is the plain string value of a ``VisualState``. Running tasks come
    from ``heartbeat.running``; without a heartbeat, from the tracker's last
    ``agent_state`` event. Pause flags come from the database.
    """
    now = clock.now()
    business_paused, agents = _load_office(conn, business_id)
    names = {agent_id: name for agent_id, name, _ in agents}
    running_map = heartbeat.running if heartbeat is not None else {}

    result: list[dict] = []
    for agent_id, _name, agent_paused in agents:
        ctx = tracker.agent_context(agent_id, now) if tracker is not None else _NO_CONTEXT
        running: Any = running_map.get(agent_id)
        if running is None and heartbeat is None and ctx.reported_state == "working":
            running = _EVENT_RUNNING

        state = visual_state(
            agent_paused,
            business_paused,
            running,
            ctx.handoff_target,
            ctx.reported_state == "waiting",
        )

        target: int | None = None
        bubble = ""
        if state is VisualState.HANDOFF:
            target = ctx.handoff_target
            bubble = ctx.handoff_text or f"Handoff to {names.get(target, target)}"
        elif state is VisualState.WORKING:
            if ctx.reported_state == "working" and ctx.bubble_text:
                bubble = ctx.bubble_text
            else:
                bubble = str(getattr(running, "reason", "") or "")
        elif state is VisualState.WAITING:
            bubble = ctx.bubble_text

        result.append(
            {
                "agent_id": agent_id,
                "state": state.value,
                "target_agent_id": target,
                "bubble_text": bubble,
            }
        )
    return result


# ---------------------------------------------------------------------- #
# Tracker
# ---------------------------------------------------------------------- #
@dataclass
class _Handoff:
    target_agent_id: int
    text: str
    expires_at: datetime


def _is_agent_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class VisualTracker:
    """Keeps per-agent visual state and publishes ``visual_state`` on every change."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        business_id: int,
        bus: EventBus,
        clock: Clock,
        heartbeat: Heartbeat | None = None,
        handoff_seconds: float = HANDOFF_SECONDS,
        poll_seconds: float = EXPIRY_POLL_SECONDS,
    ) -> None:
        self._conn = conn
        self._business_id = business_id
        self._bus = bus
        self._clock = clock
        self._handoff_seconds = handoff_seconds
        self._poll_seconds = poll_seconds
        self._reported: dict[int, tuple[str, str]] = {}  # agent_id -> (state, bubble)
        self._handoffs: dict[int, _Handoff] = {}
        self._task: asyncio.Task[None] | None = None
        if heartbeat is not None:
            # Tasks already running when the tracker is created.
            for agent_id, task in heartbeat.running.items():
                self._reported[agent_id] = ("working", str(getattr(task, "reason", "")))
        # Start from the present state, so only later changes are published.
        self._last: dict[int, dict] = {e["agent_id"]: e for e in self.current()}

    # -------------------------------------------------------------- #
    # Reading
    # -------------------------------------------------------------- #
    def current(self) -> list[dict]:
        """Current state of every agent. Reads only; publishes nothing.

        Built from the events seen so far, in order, so a task that starts and
        finishes before the tracker gets to run still shows as working first.
        """
        return snapshot(self._conn, self._business_id, None, self._clock, tracker=self)

    def agent_context(self, agent_id: int, now: datetime) -> AgentContext:
        reported = self._reported.get(agent_id)
        handoff = self._handoffs.get(agent_id)
        if handoff is not None and now >= handoff.expires_at:
            handoff = None
        return AgentContext(
            reported_state=reported[0] if reported else None,
            bubble_text=reported[1] if reported else "",
            handoff_target=handoff.target_agent_id if handoff else None,
            handoff_text=handoff.text if handoff else "",
        )

    # -------------------------------------------------------------- #
    # Reacting
    # -------------------------------------------------------------- #
    def handle_event(self, event: dict[str, Any]) -> None:
        """Apply one bus event, then publish whatever changed."""
        etype = event.get("type")
        if etype == EVENT_AGENT_STATE:
            if not self._on_agent_state(event):
                return
        elif etype == EVENT_HANDOFF:
            if not self._on_handoff(event):
                return
        elif etype != EVENT_PAUSE_CHANGED:
            return  # includes our own visual_state events
        self.refresh()

    def _on_agent_state(self, event: dict[str, Any]) -> bool:
        agent_id, state = event.get("agent_id"), event.get("state")
        if not _is_agent_id(agent_id) or state not in _REPORTABLE_STATES:
            return False
        bubble = event.get("bubble_text")
        self._reported[agent_id] = (state, bubble if isinstance(bubble, str) else "")
        self._handoffs.pop(agent_id, None)  # the next agent_state ends a handoff
        return True

    def _on_handoff(self, event: dict[str, Any]) -> bool:
        source, target = event.get("from_agent_id"), event.get("to_agent_id")
        if not _is_agent_id(source) or not _is_agent_id(target):
            return False
        text = event.get("bubble_text")
        self._handoffs[source] = _Handoff(
            target_agent_id=target,
            text=text if isinstance(text, str) else "",
            expires_at=self._clock.now() + timedelta(seconds=self._handoff_seconds),
        )
        return True

    def refresh(self) -> None:
        """Recompute every agent (pause flags, expired handoffs) and publish changes."""
        now = self._clock.now()
        for agent_id in [a for a, h in self._handoffs.items() if now >= h.expires_at]:
            del self._handoffs[agent_id]

        entries = self.current()
        known = {e["agent_id"] for e in entries}
        for agent_id in [a for a in self._last if a not in known]:
            del self._last[agent_id]
        for entry in entries:
            if self._last.get(entry["agent_id"]) == entry:
                continue
            self._last[entry["agent_id"]] = entry
            self._publish(entry)

    def _publish(self, entry: dict) -> None:
        try:
            self._bus.publish({"type": EVENT_VISUAL_STATE, **entry})
        except Exception:
            logger.exception("could not publish visual_state")

    # -------------------------------------------------------------- #
    # Background task
    # -------------------------------------------------------------- #
    def start(self) -> asyncio.Task[None]:
        """Subscribe to the bus and process events in a task. Does nothing if running."""
        if self._task is None or self._task.done():
            sub = self._bus.subscribe(maxsize=TRACKER_BUFFER)
            self._task = asyncio.get_running_loop().create_task(
                self._run(sub), name="kantor-visual-tracker"
            )
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait([task])

    async def _run(self, sub: Subscription) -> None:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(sub.get(), self._poll_seconds)
                except TimeoutError:
                    event = None  # no event: use the time to expire handoffs
                except SubscriptionClosed:
                    return
                try:
                    if event is None:
                        self.refresh()
                    else:
                        self.handle_event(event)
                except Exception:
                    logger.exception("visual tracker failed to handle an event")
        finally:
            sub.close()
