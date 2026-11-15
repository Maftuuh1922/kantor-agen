"""Owner approvals: what an agent may not decide alone waits here for an explicit, logged decision (task 14.2).

An agent that meets something outside the owner's limits (an order that does not fit today's budget, an invoice
above the auto-approve limit) does not act. It files a request with :func:`request_approval`. The owner approves
or rejects it with :func:`decide`. Nothing here knows a business type: ``kind`` is a word the app picks
("purchase_order", "invoice") and the app registers one :class:`ApprovalHandler` per kind in an
:class:`ApprovalHandlers` registry. The handler owns the domain effect (place the order, approve the invoice).

Rules enforced here, in code (Req 5.1, 5.5, 7.1, 8.2):

* **One decision.** Only a ``pending`` request can be decided. A second decision raises
  :class:`ApprovalNotPending` and changes nothing.
* **One transaction.** ``decide`` takes the write lock, runs the handler, sets the status and writes the activity
  row in one transaction. If anything fails, nothing is written and the request stays ``pending``.
* **Approval never bypasses the policy engine.** A handler that spends money calls :func:`authorize_spend`, which
  goes through ``authorize``: the ledger and today's limit stay the single source of truth. When the amount does
  not fit, :class:`ApprovalBlocked` is raised with an Indonesian message, the whole decision rolls back, and the
  request stays ``pending``. The owner can raise the limit in Aturan (Req 8.2) and approve again. An approval
  of something that spends no money (an invoice) writes no ledger row.
* **Settled without the owner.** :func:`withdraw_pending` closes a request that became moot (the rules changed and
  the thing was settled by policy). It becomes ``failed`` with no ``decided_by`` and starts no cool-down.
* **Every decision is logged.** One activity row, action ``Keputusan pemilik``, written with
  ``skeleton.log.activity.record``: the requesting agent, what was approved or rejected, and the cost (the amount
  spent when an approval spent money, else 0). A refused attempt (over the limit) writes nothing.
* **A rejection has a cool-down.** After the owner rejects something, :func:`rejected_recently` is true for
  ``REJECT_COOLDOWN`` (24 hours of the injected clock), so the agent that asked does not file the same request on
  its next tick. The apps check it in their triggers.

Handler contract: ``approve(conn, approval, clock)`` and ``reject(conn, approval, clock)`` run inside the
decision's transaction (a savepoint, so a handler may use ``transaction(conn)``). They return a
:class:`HandlerOutcome` (or ``None``) and raise :class:`ApprovalBlocked` when the request cannot be done *now but
may be later* (the request stays pending), or :class:`ApprovalFailed` when it can never be done (the request is
marked ``failed`` and logged). Any other exception rolls everything back and propagates.

``approval`` is the dict of :func:`get`: ``id, business_id, agent_id, kind, ref, subject, detail (dict),
amount_idr, status, created_at, decided_at, decided_by, decision_note``. ``amount_idr`` is the amount the request
is about; whether it is money to spend is the handler's call.

This module must not import anything from ``apps/`` or ``skeleton.runtime``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from skeleton.core.clock import Clock
from skeleton.core.db import transaction
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.policy.engine import authorize

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_FAILED})

EVENT_APPROVAL_CHANGED = "approval_changed"
DECISION_ACTION = "Keputusan pemilik"
DECIDED_BY_OWNER = "owner"
REJECT_COOLDOWN = timedelta(hours=24)
MAX_NOTE_CHARS = 200
MAX_TEXT_CHARS = 200
MAX_DETAIL_CHARS = 4000
MAX_REASON_CHARS = 400
BUBBLE_CHARS = 45

_COLUMNS = (
    "id, business_id, agent_id, kind, ref, subject, detail, amount_idr, status, "
    "created_at, decided_at, decided_by, decision_note"
)


# --------------------------------------------------------------------------- #
# Errors and results
# --------------------------------------------------------------------------- #
class ApprovalError(Exception):
    """Base class of every refusal in this module. ``str(error)`` is safe to show to the owner."""


class ApprovalNotFound(ApprovalError):
    """No such request in this business."""


class ApprovalNotPending(ApprovalError):
    """The request was decided before. ``status`` says how."""

    def __init__(self, status: str) -> None:
        super().__init__("approval is not pending")
        self.status = status


class ApprovalBlocked(ApprovalError):
    """The decision cannot be applied now (for example over the daily limit). The request stays ``pending``."""


class ApprovalFailed(ApprovalError):
    """The request can never be applied (its subject is gone or changed). It is marked ``failed`` and logged."""


@dataclass(frozen=True)
class HandlerOutcome:
    """What a handler did, in Indonesian, for the activity row. ``cost_idr`` is money spent (0 if none)."""

    message: str = ""
    cost_idr: int = 0


class ApprovalHandler(Protocol):
    def approve(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome | None: ...

    def reject(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome | None: ...


class ApprovalHandlers:
    """Registry: ``kind`` -> handler. Apps register theirs once at startup."""

    def __init__(self) -> None:
        self._by_kind: dict[str, ApprovalHandler] = {}

    def register(self, kind: str, handler: ApprovalHandler) -> None:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not callable(getattr(handler, "approve", None)) or not callable(getattr(handler, "reject", None)):
            raise ValueError("a handler needs approve() and reject()")
        self._by_kind[kind] = handler

    def get(self, kind: str) -> ApprovalHandler | None:
        return self._by_kind.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._by_kind)


@dataclass(frozen=True)
class ApprovalResult:
    """The outcome of :func:`decide`. ``status`` is ``approved``, ``rejected`` or ``failed``."""

    approval: dict
    status: str
    message: str
    cost_idr: int = 0

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAILED


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _iso(moment: datetime) -> str:
    # Same fixed format as skeleton/log/activity.py, so string order is time order.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def rupiah(amount: int) -> str:
    """Integer rupiah the Indonesian way: ``Rp 54.000``."""
    return "Rp " + f"{amount:,}".replace(",", ".")


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _require_text(name: str, value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return _clean(value, limit)


def _to_dict(row: sqlite3.Row) -> dict:
    out = {key: row[key] for key in row.keys()}
    try:
        detail = json.loads(out["detail"])
    except (TypeError, ValueError):
        detail = {}
    out["detail"] = detail if isinstance(detail, dict) else {}
    return out


class _DeferredBus:
    """Collects events while a transaction is open, so nothing is published for a decision that rolls back."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _publish(bus: EventBus | None, events: list[dict]) -> None:
    if bus is None:
        return
    for event in events:
        try:
            bus.publish(event)
        except Exception:  # a broken subscriber must not undo or hide a decision that is already saved
            pass


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def get(conn: sqlite3.Connection, business_id: int, approval_id: int) -> dict | None:
    """One request of this business, or ``None``."""
    if not _is_int(approval_id):
        return None
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM approval WHERE id = ? AND business_id = ?", (approval_id, business_id)
    ).fetchone()
    return _to_dict(row) if row else None


def list_pending(conn: sqlite3.Connection, business_id: int, agent_id: int | None = None) -> list[dict]:
    """Requests waiting for the owner, oldest first, optionally those of one agent."""
    sql = f"SELECT {_COLUMNS} FROM approval WHERE business_id = ? AND status = 'pending'"
    params: list[Any] = [business_id]
    if agent_id is not None:
        sql += " AND agent_id = ?"
        params.append(agent_id)
    sql += " ORDER BY id"
    return [_to_dict(r) for r in conn.execute(sql, params)]


def pending_count(conn: sqlite3.Connection, business_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM approval WHERE business_id = ? AND status = 'pending'", (business_id,)
    ).fetchone()
    return int(row[0])


def has_pending(conn: sqlite3.Connection, business_id: int, kind: str, ref: str) -> bool:
    """True while a request for this thing waits for the owner."""
    row = conn.execute(
        "SELECT 1 FROM approval WHERE business_id = ? AND kind = ? AND ref = ? AND status = 'pending' LIMIT 1",
        (business_id, kind, ref),
    ).fetchone()
    return row is not None


def has_request(conn: sqlite3.Connection, business_id: int, kind: str, ref: str) -> bool:
    """True when this thing was ever asked about, in any status."""
    row = conn.execute(
        "SELECT 1 FROM approval WHERE business_id = ? AND kind = ? AND ref = ? LIMIT 1", (business_id, kind, ref)
    ).fetchone()
    return row is not None


def rejected_recently(
    conn: sqlite3.Connection,
    business_id: int,
    kind: str,
    ref: str,
    clock: Clock,
    cooldown: timedelta = REJECT_COOLDOWN,
) -> bool:
    """True when the owner rejected this thing less than ``cooldown`` ago (clock time). Agents must not ask again."""
    since = _iso(clock.now() - cooldown)
    row = conn.execute(
        "SELECT 1 FROM approval WHERE business_id = ? AND kind = ? AND ref = ? AND status = 'rejected' "
        "AND decided_at > ? LIMIT 1",
        (business_id, kind, ref, since),
    ).fetchone()
    return row is not None


def waiting_agents(conn: sqlite3.Connection, business_id: int) -> dict[int, str]:
    """Agents that have a pending request, as ``{agent_id: short bubble text}`` (text of the oldest request).

    Used by the visual state: an agent with a pending request stays ``waiting`` (amber) until the owner decides.
    A database without the table (an old file before startup ran) gives ``{}``.
    """
    try:
        rows = conn.execute(
            "SELECT agent_id, subject FROM approval WHERE business_id = ? AND status = 'pending' "
            "AND agent_id IS NOT NULL ORDER BY id",
            (business_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[int, str] = {}
    for agent_id, subject in rows:
        out.setdefault(int(agent_id), _clean(f"Menunggu OK: {subject}", BUBBLE_CHARS))
    return out


# --------------------------------------------------------------------------- #
# Filing a request
# --------------------------------------------------------------------------- #
def request_approval(
    conn: sqlite3.Connection,
    business_id: int,
    agent_id: int | None,
    kind: str,
    subject: str,
    detail: dict | None,
    amount_idr: int,
    clock: Clock,
    *,
    ref: str | None = None,
    bus: EventBus | None = None,
) -> int:
    """File a request for the owner and return its id. Idempotent.

    ``ref`` names the thing asked about (default: ``subject``). While a request for the same ``(kind, ref)`` is
    pending, a second call returns the id of the first and writes nothing, so an agent that re-evaluates on every
    tick files one request, not many. ``detail`` is a JSON-able dict; by convention ``summary`` (one line) and
    ``reason`` (why the agent asks) are shown to the owner.
    """
    kind = _require_text("kind", kind, 60)
    subject = _require_text("subject", subject)
    ref = _require_text("ref", ref if ref is not None else subject, 120)
    if isinstance(amount_idr, bool) or not _is_int(amount_idr) or amount_idr < 0:
        raise ValueError("amount_idr must be a non-negative int")
    if agent_id is not None and not _is_int(agent_id):
        raise ValueError("agent_id must be an int or None")
    detail_json = json.dumps(detail if detail is not None else {}, ensure_ascii=False, sort_keys=True, default=str)
    if len(detail_json) > MAX_DETAIL_CHARS:
        raise ValueError("detail is too large")
    created = False
    with transaction(conn):
        row = conn.execute(
            "SELECT id FROM approval WHERE business_id = ? AND kind = ? AND ref = ? AND status = 'pending'",
            (business_id, kind, ref),
        ).fetchone()
        if row is not None:
            approval_id = int(row[0])
        else:
            cur = conn.execute(
                "INSERT INTO approval (business_id, agent_id, kind, ref, subject, detail, amount_idr, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (business_id, agent_id, kind, ref, subject, detail_json, amount_idr, _iso(clock.now())),
            )
            approval_id = int(cur.lastrowid)
            created = True
    if created:
        _publish(
            bus,
            [
                {
                    "type": EVENT_APPROVAL_CHANGED,
                    "approval_id": approval_id,
                    "agent_id": agent_id,
                    "status": STATUS_PENDING,
                    "pending_count": pending_count(conn, business_id),
                }
            ],
        )
    return approval_id


def withdraw_pending(
    conn: sqlite3.Connection,
    business_id: int,
    kind: str,
    ref: str,
    clock: Clock,
    note: str,
    *,
    bus: EventBus | None = None,
) -> int | None:
    """Close the pending request for ``(kind, ref)`` because the thing was settled without the owner.

    Example: the rules changed and the policy approved the invoice by itself, so the owner's question is moot.
    The request becomes ``failed`` (never ``approved``: the owner did not decide), with ``decided_by`` empty and
    ``note`` as the reason. No handler runs and no money moves. Returns the request id, or ``None`` when nothing
    was pending. The caller logs what it did itself.
    """
    note = _clean(note, MAX_NOTE_CHARS)
    with transaction(conn):
        row = conn.execute(
            "SELECT id, agent_id FROM approval WHERE business_id = ? AND kind = ? AND ref = ? AND status = 'pending'",
            (business_id, kind, ref),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE approval SET status = 'failed', decided_at = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
            (_iso(clock.now()), note, row["id"]),
        )
    _publish(
        bus,
        [
            {
                "type": EVENT_APPROVAL_CHANGED,
                "approval_id": int(row["id"]),
                "agent_id": row["agent_id"],
                "status": STATUS_FAILED,
                "pending_count": pending_count(conn, business_id),
            }
        ],
    )
    return int(row["id"])


# --------------------------------------------------------------------------- #
# The policy engine stays the gate for money
# --------------------------------------------------------------------------- #
class _SpendTask:
    """A spend task as the policy engine reads it (duck-typed ``TaskLike``)."""

    kind = "spend"

    def __init__(self, agent_id: int | None, cost_idr: int) -> None:
        self.agent_id = agent_id
        self.cost_idr = cost_idr
        self.payload: dict = {}


def authorize_spend(
    conn: sqlite3.Connection, business_id: int, agent_id: int | None, amount_idr: int, clock: Clock
) -> None:
    """Spend ``amount_idr`` through the policy engine, or raise :class:`ApprovalBlocked`.

    An approval does not override the daily limit: the amount must fit what is left today under the limit as it
    is *now*. On success one ledger row is written (in the caller's transaction). On refusal nothing is written
    and the message tells the owner what to do (Indonesian).
    """
    if not _is_int(amount_idr) or amount_idr < 0:
        raise ApprovalFailed("Jumlah tidak valid")
    decision = authorize(conn, business_id, _SpendTask(agent_id, amount_idr), clock)
    if decision.allowed:
        return
    reason = decision.reason
    if reason == "daily spending limit reached":
        row = conn.execute("SELECT daily_limit_idr FROM policy WHERE business_id = ?", (business_id,)).fetchone()
        limit = int(row[0]) if row is not None else 0
        raise ApprovalBlocked(f"Melebihi batas harian {rupiah(limit)}; naikkan batas lewat Aturan lalu setujui lagi")
    if reason == "blocked action type":
        raise ApprovalBlocked("Belanja diblokir di Aturan; buka blokirnya lewat Aturan lalu setujui lagi")
    if reason == "policy not set":
        raise ApprovalBlocked("Batas harian belum diatur; atur lewat Aturan lalu setujui lagi")
    raise ApprovalBlocked(f"Belanja ditolak aturan ({reason}); periksa Aturan lalu setujui lagi")


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #
def _decision_reason(approval: dict, approve: bool, outcome_message: str, note: str | None, failed: bool) -> str:
    amount = f" {rupiah(approval['amount_idr'])}" if approval["amount_idr"] > 0 else ""
    if failed:
        text = f"Persetujuan gagal: {approval['subject']}{amount}"
    else:
        text = f"Pemilik {'menyetujui' if approve else 'menolak'}: {approval['subject']}{amount}"
    if outcome_message:
        text += f". {outcome_message}"
    if note:
        text += f". Catatan: {note}"
    return _clean(text, MAX_REASON_CHARS)


def decide(
    conn: sqlite3.Connection,
    business_id: int,
    approval_id: int,
    approve: bool,
    handlers: ApprovalHandlers,
    clock: Clock,
    note: str | None = None,
    bus: EventBus | None = None,
) -> ApprovalResult:
    """The owner approves or rejects one pending request.

    Raises :class:`ApprovalNotFound` (unknown id or another business), :class:`ApprovalNotPending` (decided
    before), :class:`ApprovalBlocked` (cannot be applied now, for example over the limit: nothing is written and
    the request stays pending). A request whose subject can never be applied is marked ``failed``, logged, and
    returned with ``status == "failed"``. See the module docstring for the guarantees.
    """
    if not isinstance(approve, bool):
        raise ValueError("approve must be a bool")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        note = _clean(note, MAX_NOTE_CHARS) or None
    deferred = _DeferredBus()
    with transaction(conn):  # BEGIN IMMEDIATE: the write lock is held from the status check to the commit
        approval = get(conn, business_id, approval_id)
        if approval is None:
            raise ApprovalNotFound("approval not found")
        if approval["status"] != STATUS_PENDING:
            raise ApprovalNotPending(approval["status"])
        handler = handlers.get(approval["kind"])
        if handler is None:
            raise ApprovalBlocked("Jenis permintaan ini belum bisa diputuskan di aplikasi ini")
        status = STATUS_APPROVED if approve else STATUS_REJECTED
        outcome = HandlerOutcome()
        failure: ApprovalFailed | None = None
        try:
            with transaction(conn):  # a savepoint: a failed handler leaves none of its writes behind
                result = (handler.approve if approve else handler.reject)(conn, approval, clock)
                if result is not None:
                    outcome = result
        except ApprovalFailed as exc:
            failure = exc
            status = STATUS_FAILED
            outcome = HandlerOutcome(message=str(exc))
        if not isinstance(outcome, HandlerOutcome) or not _is_int(outcome.cost_idr) or outcome.cost_idr < 0:
            raise ValueError("a handler must return a HandlerOutcome with a non-negative cost")
        cost = outcome.cost_idr if (status == STATUS_APPROVED) else 0
        conn.execute(
            "UPDATE approval SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, _iso(clock.now()), DECIDED_BY_OWNER, note, approval_id),
        )
        activity_status = {STATUS_APPROVED: "done", STATUS_REJECTED: "rejected", STATUS_FAILED: "failed"}[status]
        record(
            conn,
            business_id,
            approval["agent_id"],
            DECISION_ACTION,
            _decision_reason(approval, approve, outcome.message, note, failure is not None),
            cost,
            activity_status,
            clock,
            deferred,  # type: ignore[arg-type]  # duck-typed bus: events are published after the commit
        )
        decided = get(conn, business_id, approval_id)
        assert decided is not None
        remaining = pending_count(conn, business_id)
    _publish(bus, deferred.events)
    _publish(
        bus,
        [
            {
                "type": EVENT_APPROVAL_CHANGED,
                "approval_id": approval_id,
                "agent_id": approval["agent_id"],
                "status": status,
                "pending_count": remaining,
            }
        ],
    )
    return ApprovalResult(approval=decided, status=status, message=outcome.message, cost_idr=cost)
