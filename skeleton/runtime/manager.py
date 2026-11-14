"""Manager Agent: routes tasks, picks the model, and gates money and messages.

One ``Manager`` serves one business. It handles tasks strictly one at a time, in
arrival order, because every task runs under a single ``asyncio.Lock`` (Req 4.2).
When two tasks compete (for example two spends that no longer both fit the daily
limit), the first one wins and the policy engine rejects the later one.

Order of work inside ``handle``:

1. ``spend`` and ``customer_message`` go through ``authorize`` first. A rejection
   is logged and the role is never run. The model is never asked to approve
   anything (Req 5.5).
2. Kinds that need a model (``draft`` uses ``settings.model_low``,
   ``customer_message`` uses ``settings.model_strong``) get their text from the
   Manager, passed to the role as ``task.payload["llm_text"]``. ``read`` and
   ``spend`` never call a model (Req 4.3).
3. If the model call fails, the task is marked ``rule_only``, the failure is
   logged, and the role still runs without ``llm_text`` (Req 4.4).
4. ``role.run`` is wrapped so no exception reaches the heartbeat. Every handled
   task writes an activity row.

For ``customer_message`` the outbox row is written by ``authorize`` from
``task.payload`` (``channel``, ``recipient``, ``body``) before the model is
called, so the body comes from the payload and not from ``llm_text``. Nothing is
ever sent.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.policy.engine import OUTCOME_QUEUED, authorize
from skeleton.runtime.llm import LLMClient, LLMError
from skeleton.runtime.models import ActionResult, BusinessContext, Role, Task, TaskKind

logger = logging.getLogger(__name__)

LLM_FAILURE_REASON = "LLM unavailable, continuing rule-only"
OUTBOX_MESSAGE = "queued in outbox"
LLM_TEXT_KEY = "llm_text"
HANDOFF_ACTION = "handoff"
MAX_ERROR_CHARS = 300
SYSTEM_PROMPT = (
    "You help a small business owner in Indonesia. Reply in Bahasa Indonesia, "
    "short and clear, using only the facts given."
)

# Kinds that must pass the policy engine before the role runs.
_GATED_KINDS = frozenset({TaskKind.SPEND, TaskKind.CUSTOMER_MESSAGE})


class Manager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        business_id: int,
        clock: Clock,
        llm: LLMClient,
        settings: Settings,
        roles: dict[int, Role],
        bus: EventBus | None = None,
    ) -> None:
        self._conn = conn
        self._business_id = business_id
        self._clock = clock
        self._llm = llm
        self._settings = settings
        self._roles = roles
        self._bus = bus
        self._lock = asyncio.Lock()
        self._ctx = BusinessContext(
            conn=conn, business_id=business_id, clock=clock, bus=bus, settings=settings
        )

    # ------------------------------------------------------------------ #
    # Model routing (Req 4.3)
    # ------------------------------------------------------------------ #
    def model_for(self, kind: str) -> str | None:
        """Model name for a task kind, or None when the kind never calls a model."""
        if kind == TaskKind.DRAFT:
            return self._settings.model_low
        if kind == TaskKind.CUSTOMER_MESSAGE:
            return self._settings.model_strong
        return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def handle(self, task: Task) -> ActionResult:
        """Run one task end to end. Never raises (except task cancellation)."""
        async with self._lock:
            return await self._handle_locked(task)

    async def handle_many(self, tasks: list[Task]) -> list[ActionResult]:
        """Handle tasks one after another, in the given order."""
        results: list[ActionResult] = []
        for task in tasks:
            results.append(await self.handle(task))
        return results

    async def assign(self, task: Task, to_agent_id: int) -> ActionResult:
        """Hand `task` to another agent (Req 4.1), then handle it there.

        The task's ``agent_id`` is changed to the target, so spend and outbox
        rows are attributed to the agent that actually does the work.
        """
        async with self._lock:
            from_agent_id = task.agent_id
            if to_agent_id not in self._roles:
                return self._fail(
                    from_agent_id,
                    HANDOFF_ACTION,
                    f"cannot hand off to unknown agent {to_agent_id}",
                )
            if to_agent_id != from_agent_id:
                self._log(
                    from_agent_id,
                    HANDOFF_ACTION,
                    f"agent {from_agent_id} handed task to agent {to_agent_id}: {task.reason}",
                    0,
                    "done",
                )
                self._publish(
                    {
                        "type": "handoff",
                        "from_agent_id": from_agent_id,
                        "to_agent_id": to_agent_id,
                    }
                )
                task.agent_id = to_agent_id
            return await self._handle_locked(task)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _handle_locked(self, task: Task) -> ActionResult:
        try:
            return await self._process(task)
        except Exception as exc:  # the heartbeat must never see an exception
            return self._fail(task.agent_id, task.kind, self._describe(exc))

    async def _process(self, task: Task) -> ActionResult:
        role = self._roles.get(task.agent_id)
        if role is None:
            return self._fail(task.agent_id, task.kind, f"no role for agent {task.agent_id}")
        if not isinstance(task.payload, dict):
            return self._fail(task.agent_id, task.kind, "task payload must be a dict")

        queued_in_outbox = False
        if task.kind in _GATED_KINDS:
            decision = authorize(self._conn, self._business_id, task, self._clock)
            if not decision.allowed:
                self._log(task.agent_id, task.kind, decision.reason, 0, "rejected")
                return ActionResult(status="rejected", message=decision.reason)
            queued_in_outbox = decision.outcome == OUTCOME_QUEUED

        # A caller must not be able to preset model text.
        task.payload.pop(LLM_TEXT_KEY, None)
        model = self.model_for(task.kind)
        if model is not None:
            await self._draft_text(task, model)

        try:
            result = role.run(task, self._ctx)
        except Exception as exc:
            return self._fail(task.agent_id, task.kind, f"role failed: {self._describe(exc)}")
        if not isinstance(result, ActionResult):
            return self._fail(task.agent_id, task.kind, "role returned an invalid result")

        return self._finish(task, result, queued_in_outbox)

    async def _draft_text(self, task: Task, model: str) -> None:
        """Ask the model for text. On LLMError, switch the task to rule-only."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._prompt(task)},
        ]
        try:
            text = await self._llm.complete(model, messages)
        except LLMError:
            # The error text is dropped on purpose: only the fixed reason is logged.
            task.rule_only = True
            self._log(task.agent_id, "llm", LLM_FAILURE_REASON, 0, "failed")
            return
        task.payload[LLM_TEXT_KEY] = text

    @staticmethod
    def _prompt(task: Task) -> str:
        facts = {k: v for k, v in task.payload.items() if k != LLM_TEXT_KEY}
        return f"{task.reason}\n\n{json.dumps(facts, ensure_ascii=False, default=str, sort_keys=True)}"

    def _finish(self, task: Task, result: ActionResult, queued_in_outbox: bool) -> ActionResult:
        reason = task.reason
        if queued_in_outbox and result.status != "failed":
            result = ActionResult(
                status="done", message=OUTBOX_MESSAGE, cost_idr=0, data=result.data
            )
            reason = f"{task.reason} ({OUTBOX_MESSAGE})"
        if result.status == "failed":
            reason = result.message or task.reason
        if result.status == "done" and task.kind == TaskKind.SPEND:
            # The authorized amount is the source of truth for what was spent.
            result = ActionResult(
                status="done", message=result.message, cost_idr=task.cost_idr, data=result.data
            )
        cost = result.cost_idr if result.status == "done" else 0
        self._log(task.agent_id, task.kind, reason, cost, result.status)
        return result

    def _fail(self, agent_id: int | None, action: str, reason: str) -> ActionResult:
        reason = reason or "unknown error"
        self._log(agent_id, action, reason, 0, "failed")
        return ActionResult(status="failed", message=reason)

    def _describe(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        key = self._settings.llm_api_key.strip()
        if key:
            text = text.replace(key, "***")
        return text[:MAX_ERROR_CHARS]

    def _log(self, agent_id: int | None, action: str, reason: str, cost_idr: int, status: str) -> None:
        """Write one activity row. Logging problems must not break the heartbeat."""
        args: tuple[Any, ...] = (action, reason, cost_idr, status, self._clock, self._bus)
        try:
            try:
                record(self._conn, self._business_id, agent_id, *args)
            except sqlite3.IntegrityError:
                # Unknown agent id: keep the row, drop the foreign key.
                record(self._conn, self._business_id, None, *args)
        except Exception:
            logger.exception("could not write activity row")

    def _publish(self, event: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(event)
        except Exception:
            logger.exception("could not publish event")
