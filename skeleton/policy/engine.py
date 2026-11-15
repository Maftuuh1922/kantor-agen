"""Policy engine: the only gate for money and outbound actions.

`authorize` decides whether a task may run. It runs in backend code only and
never looks at a model prompt, so model output alone cannot bypass it (Req 5.5).

Everything in `authorize` happens inside one ``BEGIN IMMEDIATE`` transaction.
The write lock is taken before the ledger is read, so two concurrent spend
requests cannot both pass the daily limit (Req 5.1).

Money is integer rupiah (never float). This module must not import from
``skeleton.runtime`` or ``apps``: tasks are duck-typed through `TaskLike`.

Callers must not already be inside a transaction on `conn` when they call
`authorize`, because it opens its own.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from skeleton.core.clock import Clock
from skeleton.core.db import transaction

ALLOWED_KINDS = frozenset({"read", "draft", "spend", "customer_message"})

OUTCOME_ALLOWED = "allowed"
OUTCOME_REJECTED = "rejected"
OUTCOME_QUEUED = "queued_in_outbox"
OUTCOMES = frozenset({OUTCOME_ALLOWED, OUTCOME_REJECTED, OUTCOME_QUEUED})

DEFAULT_CHANNEL = "unspecified"
DEFAULT_RECIPIENT = ""
DEFAULT_BODY = ""


class TaskLike(Protocol):
    """What the policy engine needs from a task. Any matching object works."""

    agent_id: int
    kind: str
    cost_idr: int
    payload: dict


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {self.outcome!r}")
        if self.allowed == (self.outcome == OUTCOME_REJECTED):
            raise ValueError("allowed and outcome are inconsistent")

    @classmethod
    def allow(cls, outcome: str = OUTCOME_ALLOWED) -> "Decision":
        return cls(allowed=True, reason="", outcome=outcome)

    @classmethod
    def reject(cls, reason: str) -> "Decision":
        return cls(allowed=False, reason=reason, outcome=OUTCOME_REJECTED)


# --------------------------------------------------------------------------- #
# Policy row access
# --------------------------------------------------------------------------- #


def set_policy(
    conn: sqlite3.Connection,
    business_id: int,
    daily_limit_idr: int,
    blocked_actions: Iterable[str],
) -> None:
    """Create or replace the policy for a business.

    Raises ValueError for a negative or non-int limit (bool is rejected) or for
    blocked actions that are not strings in ALLOWED_KINDS. It is one atomic
    statement, so it is safe to call inside or outside a caller's transaction.
    """
    if isinstance(daily_limit_idr, bool) or not isinstance(daily_limit_idr, int):
        raise ValueError("daily_limit_idr must be an integer")
    if daily_limit_idr < 0:
        raise ValueError("daily_limit_idr must not be negative")
    if isinstance(blocked_actions, (str, bytes)):
        raise ValueError("blocked_actions must be a collection of strings, not a single string")
    blocked = list(blocked_actions)
    for item in blocked:
        if not isinstance(item, str):
            raise ValueError("blocked_actions must contain only strings")
        if item not in ALLOWED_KINDS:
            raise ValueError(f"unknown action type in blocked_actions: {item!r}")
    blocked_json = json.dumps(sorted(set(blocked)))
    conn.execute(
        "INSERT INTO policy (business_id, daily_limit_idr, blocked_actions) VALUES (?, ?, ?) "
        "ON CONFLICT(business_id) DO UPDATE SET "
        "daily_limit_idr = excluded.daily_limit_idr, "
        "blocked_actions = excluded.blocked_actions",
        (business_id, daily_limit_idr, blocked_json),
    )


def _load_policy(conn: sqlite3.Connection, business_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT daily_limit_idr, blocked_actions FROM policy WHERE business_id = ?",
        (business_id,),
    ).fetchone()


def _parse_blocked(raw: Any) -> frozenset[str] | None:
    """Parse the stored JSON list. Returns None if it is malformed."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


# --------------------------------------------------------------------------- #
# Ledger queries
# --------------------------------------------------------------------------- #


def spent_today(conn: sqlite3.Connection, business_id: int, clock: Clock) -> int:
    """Total rupiah spent by the business on the clock's current Jakarta day."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger WHERE business_id = ? AND day = ?",
        (business_id, clock.today()),
    ).fetchone()
    return int(row[0])


def remaining_today(conn: sqlite3.Connection, business_id: int, clock: Clock) -> int:
    """Rupiah still available today. 0 when no policy is set or the limit is used up."""
    policy = _load_policy(conn, business_id)
    if policy is None:
        return 0
    return max(0, int(policy["daily_limit_idr"]) - spent_today(conn, business_id, clock))


# --------------------------------------------------------------------------- #
# authorize
# --------------------------------------------------------------------------- #


def _valid_cost(cost: Any) -> bool:
    return isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0


def _text(payload: dict, key: str, default: str) -> str:
    value = payload.get(key)
    if value is None:
        return default
    text = str(value)
    return text if text != "" else default


def authorize(conn: sqlite3.Connection, business_id: int, task: TaskLike, clock: Clock) -> Decision:
    """Approve or reject `task` for the business, recording what it allowed.

    Writes happen only on an allowed `spend` (one ledger row) or an allowed
    `customer_message` (one outbox row, never sent). Rejections write nothing.
    """
    kind = task.kind
    if kind not in ALLOWED_KINDS:
        return Decision.reject("unknown action type")

    with transaction(conn):
        policy = _load_policy(conn, business_id)
        if policy is None:
            if kind == "spend":
                return Decision.reject("policy not set")
            blocked: frozenset[str] = frozenset()
            daily_limit = 0
        else:
            parsed = _parse_blocked(policy["blocked_actions"])
            if parsed is None:
                # Fail closed: a corrupt blocked list must not let anything through.
                return Decision.reject("policy invalid")
            blocked = parsed
            daily_limit = int(policy["daily_limit_idr"])

        if kind in blocked:
            return Decision.reject("blocked action type")

        cost = task.cost_idr
        if not _valid_cost(cost):
            return Decision.reject("invalid cost")
        if kind != "spend" and cost != 0:
            return Decision.reject("invalid cost")

        if kind == "spend":
            day = clock.today()
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger "
                "WHERE business_id = ? AND day = ?",
                (business_id, day),
            ).fetchone()[0]
            if spent + cost > daily_limit:
                return Decision.reject("daily spending limit reached")
            conn.execute(
                "INSERT INTO spend_ledger (business_id, agent_id, amount_idr, day, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (business_id, task.agent_id, cost, day, clock.now().isoformat()),
            )
            return Decision.allow()

        if kind == "customer_message":
            payload = task.payload if isinstance(task.payload, dict) else {}
            conn.execute(
                "INSERT INTO outbox (business_id, agent_id, channel, recipient, body, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (
                    business_id,
                    task.agent_id,
                    _text(payload, "channel", DEFAULT_CHANNEL),
                    _text(payload, "recipient", DEFAULT_RECIPIENT),
                    _text(payload, "body", DEFAULT_BODY),
                ),
            )
            return Decision.allow(outcome=OUTCOME_QUEUED)

        # "read" and "draft": allowed, nothing recorded.
        return Decision.allow()
