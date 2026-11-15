"""Agent runtime models: Task, ActionResult, BusinessContext and the Role protocol.

Money is integer rupiah everywhere (`cost_idr`). Never use float or Decimal.

`Role.triggers` is pure rule logic: it reads the database and returns tasks
when a condition holds. It must not call a model (Req 3.3). Model calls happen
later, in the Manager, and only for tasks that need generated text or judgment.

This module may import `skeleton.core.*` but must not import `apps/` or the
policy engine.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from skeleton.core.clock import Clock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus


class TaskKind:
    """Task kinds as plain strings, so they match the policy engine's values."""

    READ = "read"
    DRAFT = "draft"
    SPEND = "spend"
    CUSTOMER_MESSAGE = "customer_message"

    ALL: frozenset[str] = frozenset({READ, DRAFT, SPEND, CUSTOMER_MESSAGE})


RESULT_STATUSES: frozenset[str] = frozenset({"done", "rejected", "pending", "failed", "info"})

# What the owner reads in the activity feed when a role gives no label of its own (Indonesian).
DEFAULT_ACTION_LABELS: dict[str, str] = {
    TaskKind.READ: "Periksa data",
    TaskKind.DRAFT: "Tulis draf",
    TaskKind.SPEND: "Belanja",
    TaskKind.CUSTOMER_MESSAGE: "Kirim pesan pelanggan",
}
LABEL_KEY = "label"  # payload key a role may use instead of ``Task.label``
BUBBLE_KEY = "bubble"  # payload key a role may use instead of ``Task.bubble``
MAX_LABEL_CHARS = 60
MAX_BUBBLE_CHARS = 60  # roles aim for 30 to 45; this is only the hard cap


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class Task:
    agent_id: int
    kind: str  # one of TaskKind.ALL
    payload: dict
    cost_idr: int  # integer rupiah; 0 unless kind == "spend"
    reason: str  # shown to the owner in the activity log
    rule_only: bool = False  # set by the Manager after an LLM failure (Req 4.4)
    # Friendly Indonesian action word for the activity feed, e.g. "Catat penjualan". Empty means
    # "use payload['label'], else the default of the kind". ``kind`` stays what the policy engine reads.
    label: str = ""
    # Short speech-bubble text (about 30 to 45 characters). Empty means "use payload['bubble'], else the reason".
    bubble: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TaskKind.ALL:
            raise ValueError(f"unknown task kind: {self.kind!r}")
        if not _is_plain_int(self.cost_idr):
            raise ValueError("cost_idr must be an int (integer rupiah)")
        if self.cost_idr < 0:
            raise ValueError("cost_idr must not be negative")
        if self.cost_idr != 0 and self.kind != TaskKind.SPEND:
            raise ValueError("cost_idr must be 0 unless kind is 'spend'")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(self.label, str) or not isinstance(self.bubble, str):
            raise ValueError("label and bubble must be strings")


def _clean(text: object, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def action_label(task: Task) -> str:
    """The word the activity log stores as ``action``: the task's label, else the default of its kind."""
    payload = task.payload if isinstance(task.payload, dict) else {}
    return (
        _clean(task.label, MAX_LABEL_CHARS)
        or _clean(payload.get(LABEL_KEY), MAX_LABEL_CHARS)
        or DEFAULT_ACTION_LABELS.get(task.kind, task.kind)
    )


def task_bubble(task: Any) -> str:
    """Text for the speech bubble while the task runs: the role's short text, else the reason."""
    payload = getattr(task, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    short = _clean(getattr(task, "bubble", ""), MAX_BUBBLE_CHARS) or _clean(payload.get(BUBBLE_KEY), MAX_BUBBLE_CHARS)
    return short or str(getattr(task, "reason", "") or "")


@dataclass
class ActionResult:
    status: str  # "done" | "rejected" | "pending" | "failed" | "info"
    message: str
    cost_idr: int = 0
    data: dict | None = None
    bubble: str = ""  # short bubble for a pending result (agent waits for the owner); empty means "use message"

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"unknown result status: {self.status!r}")
        if not _is_plain_int(self.cost_idr) or self.cost_idr < 0:
            raise ValueError("cost_idr must be a non-negative int")
        if not isinstance(self.bubble, str):
            raise ValueError("bubble must be a string")


@dataclass
class BusinessContext:
    conn: sqlite3.Connection
    business_id: int
    clock: Clock
    bus: EventBus | None = None
    settings: Settings | None = None


@runtime_checkable
class Role(Protocol):
    name: str

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        """Rule-only check of the database. Must make no model call."""
        ...

    def run(self, task: Task, ctx: BusinessContext) -> ActionResult | Awaitable[ActionResult]:
        """Do the task. May be ``async def``: the Manager awaits an awaitable result."""
        ...


class BaseRole:
    """Helper base class for apps: no triggers, and `run` returns an info result."""

    name: str = ""
    # Task kinds of this role that need a model (generated text or judgment).
    needs_llm_kinds: frozenset[str] = frozenset()

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        return []

    def run(self, task: Task, ctx: BusinessContext) -> ActionResult:
        return ActionResult(status="info", message=f"{self.name or type(self).__name__}: nothing to do")
