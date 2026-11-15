"""Manager Agent: routes tasks, picks the model, and gates money and messages.

One ``Manager`` serves one business. It handles tasks strictly one at a time, in
arrival order, because every task runs under a single ``asyncio.Lock`` (Req 4.2).
When two tasks compete (for example two spends that no longer both fit the daily
limit), the first one wins and the policy engine rejects the later one.

Order of work inside ``handle``:

1. ``spend`` and ``customer_message`` go through ``authorize`` before the role
   runs. A rejection is logged and the role is never run. The model is never
   asked to approve anything (Req 5.5).
   One exception to the order: a ``customer_message`` whose payload has no
   non-empty ``body`` gets its text from the model first, so the outbox row that
   ``authorize`` writes holds the drafted text. If the model fails, the body
   stays as the role's rule-based template, or becomes a short neutral default
   when there is none, so the outbox is never empty.
2. Kinds that need a model (``draft`` uses ``settings.model_low``,
   ``customer_message`` uses ``settings.model_strong``) get their text from the
   Manager, passed to the role as ``task.payload["llm_text"]``. ``read`` and
   ``spend`` never call a model (Req 4.3). A task never triggers more than one
   model call.
3. If the model call fails, the task is marked ``rule_only``, the failure is
   logged, and the role still runs without ``llm_text`` (Req 4.4).
4. Optional pacing: when ``settings.pacing_enabled`` is set (or a ``Pacer`` is passed in),
   ``Pacer.wait`` holds the task for a few seconds before step 1, while the heartbeat
   reports it as running. This is "think time" for the live apps, not compute: no extra
   model call is made. Off by default, see ``pacing.py`` and ``docs/pacing.md``.
5. The activity row's ``action`` is the task's friendly Indonesian label (``Task.label``,
   else ``payload["label"]``, else a default for the kind, see ``models.action_label``), for
   example "Catat penjualan". The task ``kind`` is still what the policy engine reads. A
   rejected task logs a row that says what was rejected, how much and why (``_rejection_reason``).
6. ``role.run`` is wrapped so no exception reaches the heartbeat. Every handled
   task writes an activity row. ``run`` may be a plain function or ``async def``;
   an awaitable result is awaited while the Manager lock is held.

For ``customer_message`` the outbox row is written by ``authorize`` from
``task.payload`` (``channel``, ``recipient``, ``body``). Nothing is ever sent.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from typing import Any

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.policy.engine import OUTCOME_QUEUED, Decision, authorize
from skeleton.runtime.llm import LLMClient, LLMError
from skeleton.runtime.models import ActionResult, BusinessContext, Role, Task, TaskKind, action_label
from skeleton.runtime.pacing import Pacer

logger = logging.getLogger(__name__)

LLM_FAILURE_REASON = "Model AI tidak tersedia, lanjut tanpa model"
LLM_FAILURE_ACTION = "Model AI"
OUTBOX_MESSAGE = "queued in outbox"  # the result message (the API contract)
OUTBOX_NOTE = "masuk outbox, belum dikirim"  # what the activity row says (Indonesian)
LLM_TEXT_KEY = "llm_text"
HANDOFF_ACTION = "Serah tugas"
MAX_REASON_CHARS = 400
BODY_KEY = "body"
DEFAULT_BODY_TEMPLATE = "Pesan dari {name}: lihat detail di kantor."
DEFAULT_SENDER_NAME = "agen"
MAX_ERROR_CHARS = 300
SYSTEM_PROMPT = (
    "You help a small business owner in Indonesia. Reply in Bahasa Indonesia, "
    "short and clear, using only the facts given."
)

_REJECTION_WORDS = {
    "blocked action type": "jenis aksi ini diblokir pemilik",
    "policy not set": "aturan belanja belum diatur pemilik",
    "policy invalid": "aturan belanja tidak valid",
    "invalid cost": "biaya tidak valid",
    "unknown action type": "jenis aksi tidak dikenal",
}


def _rupiah(amount: int) -> str:
    return "Rp " + f"{amount:,}".replace(",", ".")


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
        pacer: Pacer | None = None,
    ) -> None:
        self._conn = conn
        self._business_id = business_id
        self._clock = clock
        self._llm = llm
        self._settings = settings
        self._roles = roles
        self._bus = bus
        self._lock = asyncio.Lock()
        # An explicit pacer wins (tests); otherwise settings decide. None means no pacing.
        self._pacer = pacer if pacer is not None else Pacer.from_settings(settings)
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
                    f"tidak bisa menyerahkan tugas ke staf yang tidak dikenal (#{to_agent_id})",
                )
            if to_agent_id != from_agent_id:
                self._log(
                    from_agent_id,
                    HANDOFF_ACTION,
                    f"{self._agent_name(from_agent_id)} menyerahkan tugas ke "
                    f"{self._agent_name(to_agent_id)}: {task.reason}",
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
                return await self._handle_locked(task, origin_agent_id=from_agent_id)
            return await self._handle_locked(task)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _handle_locked(self, task: Task, origin_agent_id: int | None = None) -> ActionResult:
        try:
            return await self._process(task, origin_agent_id)
        except Exception as exc:  # the heartbeat must never see an exception
            return self._fail(task.agent_id, action_label(task), self._describe(exc))

    async def _process(self, task: Task, origin_agent_id: int | None = None) -> ActionResult:
        role = self._roles.get(task.agent_id)
        if role is None:
            return self._fail(task.agent_id, action_label(task), f"no role for agent {task.agent_id}")
        if not isinstance(task.payload, dict):
            return self._fail(task.agent_id, action_label(task), "task payload must be a dict")

        await self._pace(task, role, origin_agent_id)

        # A caller must not be able to preset model text.
        task.payload.pop(LLM_TEXT_KEY, None)
        model = self.model_for(task.kind)
        model_called = False
        if task.kind == TaskKind.CUSTOMER_MESSAGE and not self._has_body(task.payload):
            # The outbox row is written by authorize, so the text must exist first.
            model_called = True
            if model is not None:
                text = await self._draft_text(task, model)
                if text is not None and text.strip():
                    task.payload[BODY_KEY] = text
            if not self._has_body(task.payload):
                name = (getattr(role, "name", "") or "").strip() or DEFAULT_SENDER_NAME
                task.payload[BODY_KEY] = DEFAULT_BODY_TEMPLATE.format(name=name)
        queued_in_outbox = False
        if task.kind in _GATED_KINDS:
            decision = authorize(self._conn, self._business_id, task, self._clock)
            if not decision.allowed:
                self._log(task.agent_id, action_label(task), self._rejection_reason(task, decision), 0, "rejected")
                return ActionResult(status="rejected", message=decision.reason)
            queued_in_outbox = decision.outcome == OUTCOME_QUEUED
        if model is not None and not model_called:
            await self._draft_text(task, model)
        try:
            result = role.run(task, self._ctx)
            if inspect.isawaitable(result):
                # An async role (``async def run``) may wait without blocking the event loop.
                # The Manager lock is still held, so tasks stay strictly one at a time.
                result = await result
        except Exception as exc:
            return self._fail(task.agent_id, action_label(task), f"role failed: {self._describe(exc)}")
        if not isinstance(result, ActionResult):
            return self._fail(task.agent_id, action_label(task), "role returned an invalid result")

        return self._finish(task, result, queued_in_outbox)

    async def _pace(self, task: Task, role: Role, origin_agent_id: int | None) -> None:
        """Hold the task for its think time. A pacing problem never fails the task."""
        if self._pacer is None:
            return
        watch = () if origin_agent_id is None else (origin_agent_id,)
        try:
            await self._pacer.wait(task, role, self._ctx, watch_agents=watch)
        except Exception:
            logger.exception("pacing failed; continuing without the wait")

    @staticmethod
    def _has_body(payload: dict) -> bool:
        value = payload.get(BODY_KEY)
        return value is not None and str(value).strip() != ""

    async def _draft_text(self, task: Task, model: str) -> str | None:
        """Ask the model for text. On LLMError, switch the task to rule-only.

        Returns the text, or None when the model failed.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._prompt(task)},
        ]
        try:
            text = await self._llm.complete(model, messages)
        except LLMError:
            # The error text is dropped on purpose: only the fixed reason is logged.
            task.rule_only = True
            self._log(task.agent_id, LLM_FAILURE_ACTION, LLM_FAILURE_REASON, 0, "failed")
            return None
        task.payload[LLM_TEXT_KEY] = text
        return text

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
            reason = f"{task.reason} ({OUTBOX_NOTE})"
        if result.status == "failed":
            reason = result.message or task.reason
        if result.status == "done" and task.kind == TaskKind.SPEND:
            # The authorized amount is the source of truth for what was spent.
            result = ActionResult(
                status="done", message=result.message, cost_idr=task.cost_idr, data=result.data
            )
        cost = result.cost_idr if result.status == "done" else 0
        self._log(task.agent_id, action_label(task), reason, cost, result.status)
        return result

    # ------------------------------------------------------------------ #
    # What the owner reads when something is rejected
    # ------------------------------------------------------------------ #
    def _why_rejected(self, decision: Decision) -> str:
        """The policy's reason in Indonesian, with the daily limit when that is the reason."""
        reason = decision.reason
        if reason == "daily spending limit reached":
            row = self._conn.execute(
                "SELECT daily_limit_idr FROM policy WHERE business_id = ?", (self._business_id,)
            ).fetchone()
            if row is not None:
                return f"batas harian {_rupiah(int(row[0]))} tercapai"
            return "batas harian tercapai"
        return _REJECTION_WORDS.get(reason, reason)

    def _rejection_reason(self, task: Task, decision: Decision) -> str:
        """For example ``Pesan stok Teh Botol Rp 232.500 ditolak: batas harian Rp 100.000 tercapai. <task reason>``.

        Names the action, its subject (``payload["subject"]``) and the amount, says why, and ends with the
        task's own reason. The text never contains the API key.
        """
        parts = [action_label(task)]
        subject = task.payload.get("subject") if isinstance(task.payload, dict) else None
        if isinstance(subject, str) and subject.strip():
            parts.append(subject.strip())
        if task.kind == TaskKind.SPEND and task.cost_idr > 0:
            parts.append(_rupiah(task.cost_idr))
        text = f"{' '.join(parts)} ditolak: {self._why_rejected(decision)}."
        if task.reason.strip():
            text += f" Tugas: {task.reason.strip()}"
        key = self._settings.llm_api_key.strip()
        if key:
            text = text.replace(key, "***")
        return text[:MAX_REASON_CHARS]

    def _agent_name(self, agent_id: int) -> str:
        try:
            row = self._conn.execute(
                "SELECT name FROM agent WHERE id = ? AND business_id = ?", (agent_id, self._business_id)
            ).fetchone()
        except sqlite3.Error:
            row = None
        return str(row[0]) if row is not None and row[0] else f"staf #{agent_id}"

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
