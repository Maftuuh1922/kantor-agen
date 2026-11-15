"""Work pacing ("think time"): a short, honest wait inside a task (Req 2.5, 2.8, 3.2).

Why it exists: with the offline fake model and rule-based roles, almost every task
finishes in a few milliseconds. The agent would be ``working`` for a blink, so the
owner would never see anyone sit down and type or walk to a colleague. Pacing holds
a task in the running state for a few seconds so the office can show it.

It is pacing, not compute. Nothing is computed during the wait and no extra model
call is made. The scene stays truthful because the task really is running in the
backend for the whole wait: ``Heartbeat.running`` holds it and the ``working``
agent_state event is already published when the wait starts (Req 2.8).
See ``docs/pacing.md``.

Where it runs: ``Manager._process`` calls ``Pacer.wait`` first thing for a task whose
agent has a role, under the Manager lock, so the result, the policy decision and the
activity row all come after the wait. For a handoff the Manager has already published
the ``handoff`` event, so the walk to the colleague is visible during the wait.
A task that fails before the role runs (no role, bad payload) is not paced.

Duration: deterministic. It comes from the clock day, the agent id, the task kind and
the task reason, hashed with SHA-256, so no ``random`` module is involved and the same
task on the same day always waits the same time. The hash picks a point inside a band
of the configured ``min..max`` range, by task kind:

    read                0%  to 60% of the range   (a quick look)
    spend, draft       60%  to 85%                (a decision or a short text)
    customer_message   85%  to 100%               (the model drafts a message)

The bands do not overlap, so ``read < spend/draft < customer_message`` whenever
``max > min``. A handoff task adds ``HANDOFF_WALK_SECONDS`` (never more than ``max``)
for the walk.

Pause (Req 8.3): the wait is cut into steps of ``poll_seconds`` (0.25 s). Before each
step the pacer reads the pause flags of the business and of the agent from the
database. When either is paused, the rest of the wait is skipped. The task still
finishes its current step, and the heartbeat starts no new task.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable

from skeleton.core.config import Settings
from skeleton.runtime.models import BusinessContext, Role, Task, TaskKind

logger = logging.getLogger(__name__)

HANDOFF_KEY = "handoff_to"  # payload key that makes the Heartbeat hand a task to a colleague
HANDOFF_WALK_SECONDS = 3.0
POLL_SECONDS = 0.25

# kind -> (low, high) as fractions of the min..max range
_BANDS: dict[str, tuple[float, float]] = {
    TaskKind.READ: (0.0, 0.6),
    TaskKind.SPEND: (0.6, 0.85),
    TaskKind.DRAFT: (0.6, 0.85),
    TaskKind.CUSTOMER_MESSAGE: (0.85, 1.0),
}

SleepFn = Callable[[float], Awaitable[None]]


def _fraction(*parts: object) -> float:
    """Deterministic number in [0, 1) from the parts. No ``random``, no clock reads."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class Pacer:
    def __init__(
        self,
        min_seconds: float,
        max_seconds: float,
        sleep: SleepFn | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        # Settings holds the one copy of the range rules (0 <= min <= max <= 60).
        Settings(pacing_min_seconds=min_seconds, pacing_max_seconds=max_seconds)
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        self.min_seconds = float(min_seconds)
        self.max_seconds = float(max_seconds)
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._poll = float(poll_seconds)

    @classmethod
    def from_settings(cls, settings: Settings, sleep: SleepFn | None = None) -> Pacer | None:
        """A Pacer for ``settings``, or None when pacing is off."""
        if not settings.pacing_enabled:
            return None
        return cls(settings.pacing_min_seconds, settings.pacing_max_seconds, sleep=sleep)

    # ------------------------------------------------------------------ #
    def duration(self, task: Task, ctx: BusinessContext) -> float:
        """Seconds this task waits. Deterministic for (day, agent, kind, reason)."""
        low, high = _BANDS.get(task.kind, (0.0, 1.0))
        span = self.max_seconds - self.min_seconds
        frac = _fraction(ctx.clock.today(), task.agent_id, task.kind, task.reason)
        seconds = self.min_seconds + span * (low + (high - low) * frac)
        if _is_handoff(task):
            seconds += min(HANDOFF_WALK_SECONDS, self.max_seconds)
        return seconds

    async def wait(
        self,
        task: Task,
        role: Role | None,
        ctx: BusinessContext,
        watch_agents: Iterable[int] = (),
    ) -> float:
        """Hold the task for its think time. Returns the seconds actually waited.

        ``watch_agents`` are extra agents whose pause flag also ends the wait (the
        agent that handed the task over, because it is the one shown as running).
        Cancellation passes through, so stopping the heartbeat stops the wait.
        """
        remaining = self.duration(task, ctx)
        agents = {task.agent_id, *watch_agents}
        waited = 0.0
        while remaining > 1e-9:
            if self._paused(ctx, agents):
                break
            step = min(self._poll, remaining)
            await self._sleep(step)
            remaining -= step
            waited += step
        return waited

    @staticmethod
    def _paused(ctx: BusinessContext, agent_ids: set[int]) -> bool:
        """True when the business or one of the agents is paused (or unreadable: fail safe)."""
        try:
            row = ctx.conn.execute(
                "SELECT paused FROM business WHERE id = ?", (ctx.business_id,)
            ).fetchone()
            if row is None or row[0]:
                return True
            for agent_id in agent_ids:
                row = ctx.conn.execute(
                    "SELECT paused FROM agent WHERE id = ? AND business_id = ?",
                    (agent_id, ctx.business_id),
                ).fetchone()
                if row is not None and row[0]:
                    return True
        except Exception:
            logger.exception("pacing could not read the pause flags")
            return True
        return False


def _is_handoff(task: Task) -> bool:
    payload = task.payload
    target = payload.get(HANDOFF_KEY) if isinstance(payload, dict) else None
    return isinstance(target, int) and not isinstance(target, bool)


def settings_with_pacing(
    settings: Settings,
    enabled: bool = True,
    min: float | None = None,  # noqa: A002 - the name is part of the agreed helper signature
    max: float | None = None,  # noqa: A002
) -> Settings:
    """Copy of ``settings`` with pacing switched on or off and, optionally, a new range.

    ``min``/``max`` default to the values already in ``settings``. The result is
    validated (0 <= min <= max <= 60). A live app calls this once, before building
    the Manager and the Heartbeat; ``Manager`` then paces its tasks by itself.
    """
    changes: dict[str, object] = {"pacing_enabled": enabled}
    if min is not None:
        changes["pacing_min_seconds"] = min
    if max is not None:
        changes["pacing_max_seconds"] = max
    return dataclasses.replace(settings, **changes)  # type: ignore[arg-type]
