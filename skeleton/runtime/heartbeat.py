"""Heartbeat: evaluates every agent's triggers on a fixed schedule (Req 3.1, 3.2).

One ``tick`` is one evaluation cycle:

1. If the business is paused, return at once and start nothing (Req 3.4).
2. For each agent that is not paused and has a role, call ``role.triggers``.
   This is pure rule logic, no model call (Req 3.3). An exception in one agent is
   logged as a ``failed`` activity row and the loop moves on to the next agent.
3. Hand the tasks to the Manager one at a time, in agent order. The business and
   agent pause flags are read again before every task, so an owner pause stops
   new tasks at once while the task already running finishes (Req 8.3).

``running`` maps agent id to the task being handled right now. The visual state
function reads it. Every start and finish also publishes an ``agent_state``
event on the bus.

``run_forever`` waits ``settings.heartbeat_seconds`` between ticks and never dies
from an exception inside a tick. It awaits each tick before it sleeps, so ticks never
overlap: with work pacing on (``pacing.py``) a tick lasts as long as the sum of its
tasks' think times, and the next tick starts one interval after it ended. ``tick``
also holds a lock, so a manual tick cannot overlap a running one either.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.runtime.manager import Manager
from skeleton.runtime.models import ActionResult, BusinessContext, Role, Task
from skeleton.runtime.pacing import HANDOFF_KEY  # defined in pacing.py; the demo imports it from here

logger = logging.getLogger(__name__)

STATE_WORKING = "working"
STATE_IDLE = "idle"
STATE_WAITING = "waiting"
TRIGGER_ACTION = "triggers"
MAX_ERROR_CHARS = 300

SleepFn = Callable[[float], Awaitable[None]]


class Heartbeat:
    def __init__(
        self,
        conn: sqlite3.Connection,
        business_id: int,
        clock: Clock,
        manager: Manager,
        roles: dict[int, Role],
        settings: Settings,
        bus: EventBus | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._conn = conn
        self._business_id = business_id
        self._clock = clock
        self._manager = manager
        self._roles = roles
        self._settings = settings
        self._bus = bus
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._tick_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._ctx = BusinessContext(
            conn=conn, business_id=business_id, clock=clock, bus=bus, settings=settings
        )
        # agent_id -> task being handled right now (read by visual_state)
        self.running: dict[int, Task] = {}

    # ------------------------------------------------------------------ #
    # One evaluation cycle
    # ------------------------------------------------------------------ #
    async def tick(self) -> list[ActionResult]:
        """Evaluate triggers once and handle the resulting tasks."""
        async with self._tick_lock:
            if self._business_paused():
                return []

            tasks: list[Task] = []
            for agent_id in self._agent_ids():
                if self._business_paused():
                    return []
                role = self._roles.get(agent_id)
                if role is None or self._agent_paused(agent_id):
                    continue
                try:
                    tasks.extend(role.triggers(self._ctx))
                except Exception as exc:
                    self._log_failure(agent_id, TRIGGER_ACTION, f"triggers failed: {self._describe(exc)}")

            results: list[ActionResult] = []
            for task in tasks:
                # Pause is read before every task step (Req 8.3).
                if self._business_paused():
                    break
                if self._agent_paused(task.agent_id):
                    continue
                results.append(await self._run_task(task))
            return results

    async def _run_task(self, task: Task) -> ActionResult:
        agent_id = task.agent_id
        state = STATE_IDLE
        bubble = ""
        self.running[agent_id] = task
        try:
            self._publish_state(agent_id, STATE_WORKING, task.reason)
            try:
                target = self._handoff_target(task)
                if target is None:
                    result = await self._manager.handle(task)
                else:
                    # A handoff goes through assign so it reaches the log and the bus.
                    result = await self._manager.assign(task, target)
            except Exception as exc:  # the Manager should not raise, but never trust that
                message = f"task failed: {self._describe(exc)}"
                self._log_failure(agent_id, task.kind, message)
                result = ActionResult(status="failed", message=message)
            if result.status == "pending":
                state, bubble = STATE_WAITING, result.message
            return result
        finally:
            self.running.pop(agent_id, None)
            self._publish_state(agent_id, state, bubble)

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    async def run_forever(self) -> None:
        """Tick, sleep ``heartbeat_seconds``, repeat. A long tick delays the next one; never overlaps.

        Exceptions in a tick are logged, not raised.
        """
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("heartbeat tick failed")
            await self._sleep(self._settings.heartbeat_seconds)

    def start(self) -> asyncio.Task[None]:
        """Start the loop as an asyncio task. Does nothing if it already runs."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self.run_forever(), name="kantor-heartbeat"
            )
        return self._task

    async def stop(self) -> None:
        """Cancel the loop and wait until it has finished."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait([task])

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _handoff_target(task: Task) -> int | None:
        """Agent id in ``payload["handoff_to"]``, or None when there is no valid one."""
        payload = task.payload
        target = payload.get(HANDOFF_KEY) if isinstance(payload, dict) else None
        if isinstance(target, int) and not isinstance(target, bool):
            return target
        return None

    def _business_paused(self) -> bool:
        """True when paused. An unknown business counts as paused (fail safe)."""
        row = self._conn.execute(
            "SELECT paused FROM business WHERE id = ?", (self._business_id,)
        ).fetchone()
        return row is None or bool(row[0])

    def _agent_paused(self, agent_id: int) -> bool:
        row = self._conn.execute(
            "SELECT paused FROM agent WHERE id = ? AND business_id = ?",
            (agent_id, self._business_id),
        ).fetchone()
        # An unknown agent is left to the Manager, which logs a failed row.
        return row is not None and bool(row[0])

    def _agent_ids(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM agent WHERE business_id = ? ORDER BY id", (self._business_id,)
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _describe(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        key = self._settings.llm_api_key.strip()
        if key:
            text = text.replace(key, "***")
        return text[:MAX_ERROR_CHARS]

    def _log_failure(self, agent_id: int, action: str, reason: str) -> None:
        """Write a failed activity row. Logging problems must not stop the loop."""
        try:
            record(
                self._conn, self._business_id, agent_id, action, reason, 0, "failed",
                self._clock, self._bus,
            )
        except Exception:
            logger.exception("could not write activity row")

    def _publish_state(self, agent_id: int, state: str, bubble_text: str) -> None:
        if self._bus is None:
            return
        event: dict[str, Any] = {
            "type": "agent_state",
            "agent_id": agent_id,
            "state": state,
            "bubble_text": bubble_text,
        }
        try:
            self._bus.publish(event)
        except Exception:
            logger.exception("could not publish event")
