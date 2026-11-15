"""Append-only activity log and daily summary (Req 7.1, 7.2, 7.3).

Every agent action is written with :func:`record`. The module exposes no update
or delete function. As a second line of defence, :func:`install_append_only_triggers`
adds SQLite triggers that abort any ``UPDATE`` or ``DELETE`` on ``activity``.
**App startup must call** ``install_append_only_triggers(conn)`` once per
database after ``init_schema``; it is idempotent. ``record`` does not call it.

Timestamps are stored as UTC ISO-8601 strings with a fixed format (microseconds,
``+00:00`` offset) so that string comparison matches time order. Daily summaries
convert the local (Asia/Jakarta) day to a UTC range instead of slicing strings.

This module must not import anything from ``apps/``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from skeleton.core.clock import Clock
from skeleton.core.events import EventBus

ALLOWED_STATUSES = frozenset({"done", "rejected", "pending", "failed", "info"})
DEFAULT_TZ = "Asia/Jakarta"

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS activity_no_update
BEFORE UPDATE ON activity
BEGIN
    SELECT RAISE(ABORT, 'activity log is append-only: UPDATE not allowed');
END;

CREATE TRIGGER IF NOT EXISTS activity_no_delete
BEFORE DELETE ON activity
BEGIN
    SELECT RAISE(ABORT, 'activity log is append-only: DELETE not allowed');
END;
"""


def install_append_only_triggers(conn: sqlite3.Connection) -> None:
    """Block UPDATE and DELETE on ``activity``. Safe to call repeatedly.

    Call this from app startup after ``db.init_schema(conn)``.
    """
    conn.executescript(_TRIGGERS)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def record(
    conn: sqlite3.Connection,
    business_id: int,
    agent_id: int | None,
    action: str,
    reason: str,
    cost_idr: int,
    status: str,
    clock: Clock,
    bus: EventBus | None = None,
) -> int:
    """Insert one activity row and return its id.

    Raises ``ValueError`` for an empty action or reason, a negative or
    non-integer cost, or a status outside :data:`ALLOWED_STATUSES`. When ``bus``
    is given, an ``activity`` event is published after the insert.
    """
    _require_text("action", action)
    _require_text("reason", reason)
    if not _is_int(cost_idr) or cost_idr < 0:
        raise ValueError("cost_idr must be a non-negative int")
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALLOWED_STATUSES)}")

    created_at = _iso(clock.now())
    cur = conn.execute(
        "INSERT INTO activity (business_id, agent_id, action, reason, cost_idr, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (business_id, agent_id, action, reason, cost_idr, status, created_at),
    )
    row_id = int(cur.lastrowid)

    if bus is not None:
        bus.publish(
            {
                "type": "activity",
                "id": row_id,
                "business_id": business_id,
                "agent_id": agent_id,
                "action": action,
                "reason": reason,
                "cost_idr": cost_idr,
                "status": status,
                "created_at": created_at,
            }
        )
    return row_id


def recent(
    conn: sqlite3.Connection,
    business_id: int,
    limit: int = 50,
    agent_id: int | None = None,
) -> list[dict]:
    """Return the newest activity rows first, optionally for one agent."""
    if not _is_int(limit) or limit < 0:
        raise ValueError("limit must be a non-negative int")
    sql = (
        "SELECT id, business_id, agent_id, action, reason, cost_idr, status, created_at "
        "FROM activity WHERE business_id = ?"
    )
    params: list[Any] = [business_id]
    if agent_id is not None:
        sql += " AND agent_id = ?"
        params.append(agent_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def _day_range_utc(day: str | date, tz_name: str) -> tuple[str, str, str]:
    """Return (day_iso, start_utc_iso, end_utc_iso) for a local calendar day."""
    if isinstance(day, datetime):
        raise ValueError("day must be a date or a YYYY-MM-DD string")
    if isinstance(day, date):
        local_day = day
    else:
        try:
            local_day = date.fromisoformat(day)
        except (TypeError, ValueError):
            raise ValueError("day must be a date or a YYYY-MM-DD string") from None
    tz = ZoneInfo(tz_name)
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)
    return local_day.isoformat(), _iso(start), _iso(end)


def daily_summary(
    conn: sqlite3.Connection,
    business_id: int,
    day: str | date,
    clock_tz: str = DEFAULT_TZ,
) -> dict:
    """Summarise one local day: action counts, spend, and pending items.

    ``total_spent_idr`` sums ``cost_idr`` of ``done`` rows only. ``pending``
    lists rows with status ``pending`` (oldest first).
    """
    day_iso, start, end = _day_range_utc(day, clock_tz)
    where = "business_id = ? AND created_at >= ? AND created_at < ?"
    args = (business_id, start, end)

    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM activity WHERE {where} GROUP BY status", args
        )
    }
    spent = conn.execute(
        f"SELECT COALESCE(SUM(cost_idr), 0) FROM activity WHERE {where} AND status = 'done'",
        args,
    ).fetchone()[0]
    pending = [
        dict(r)
        for r in conn.execute(
            f"SELECT id, agent_id, action, reason FROM activity "
            f"WHERE {where} AND status = 'pending' ORDER BY created_at, id",
            args,
        )
    ]
    return {
        "day": day_iso,
        "total_actions": sum(by_status.values()),
        "by_status": by_status,
        "total_spent_idr": int(spent),
        "pending": pending,
    }
