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


@dataclass
class ActionResult:
    status: str  # "done" | "rejected" | "pending" | "failed" | "info"
    message: str
    cost_idr: int = 0
    data: dict | None = None

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"unknown result status: {self.status!r}")
        if not _is_plain_int(self.cost_idr) or self.cost_idr < 0:
            raise ValueError("cost_idr must be a non-negative int")


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
