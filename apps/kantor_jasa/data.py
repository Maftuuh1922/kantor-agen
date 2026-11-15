"""Jasa data layer: customers, bookings, invoices.

Money is integer rupiah. Every write function validates its input first and
then writes inside ``transaction()``, so it is all-or-nothing: if it raises,
nothing was written.

Bookings
    ``requested -> confirmed | cancelled``, ``confirmed -> cancelled | done``.
    A ``requested`` booking can also be moved to another start time
    (``reschedule_booking``). A booking can be confirmed only when no other *confirmed* booking on the
    same resource overlaps it. Intervals are half-open, ``[start, start +
    duration)``, so back-to-back bookings are fine. The check and the update run
    in one ``BEGIN IMMEDIATE`` transaction, so two writers cannot both confirm
    overlapping bookings. Resource names are compared exactly (after trimming).

Invoices
    ``draft -> approved | void``, ``approved -> paid | void``. After approval an
    invoice cannot change. This is enforced by database triggers in
    :mod:`apps.kantor_jasa.schema`, not only by the functions here.

Times are stored as UTC ISO-8601 strings with microseconds. Days (reports,
``list_bookings``, working hours) are Asia/Jakarta days.

Depends only on ``skeleton.*`` and this app's own schema; the skeleton never
imports this module.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from skeleton.core.clock import JAKARTA, Clock
from skeleton.core.db import transaction

from .schema import APPROVERS, BOOKING_STATUSES, INVOICE_STATUSES, MAX_DURATION_MIN

SLOT_STEP = timedelta(minutes=30)
DEFAULT_HOURS = (9, 17)
MAX_SEARCH_DAYS = 30  # next_free_slot looks at most this many days past `after`
_MAX_DURATION = timedelta(minutes=MAX_DURATION_MIN)


class JasaError(ValueError):
    """Base class for every rejection raised by this module."""


class InvalidInput(JasaError):
    """An argument has the wrong type, is empty, or is out of range."""


class UnknownBusinessError(JasaError):
    """The business does not exist."""


class UnknownCustomerError(JasaError):
    """The customer does not exist or belongs to another business."""


class UnknownBookingError(JasaError):
    """The booking does not exist or belongs to another business."""


class UnknownInvoiceError(JasaError):
    """The invoice does not exist or belongs to another business."""


class BookingConflict(JasaError):
    """Another confirmed booking on the same resource overlaps this one."""


class BookingStateError(JasaError):
    """The booking is not in a status that allows this change."""


class InvoiceStateError(JasaError):
    """The invoice is not in a status that allows this change."""


# --------------------------------------------------------------- validation --


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_id(name: str, value: Any) -> int:
    if not _is_int(value):
        raise InvalidInput(f"{name} must be an int")
    return value


def _require_amount(name: str, value: Any) -> int:
    if not _is_int(value) or value < 0:
        raise InvalidInput(f"{name} must be a non-negative int (whole rupiah)")
    return value


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{name} must be a non-empty string")
    return value.strip()


def _require_duration(value: Any) -> int:
    if not _is_int(value) or not 1 <= value <= MAX_DURATION_MIN:
        raise InvalidInput(f"duration_min must be an int from 1 to {MAX_DURATION_MIN}")
    return value


def _parse_moment(name: str, value: Any) -> datetime:
    """Accept an ISO-8601 string with a UTC offset, or an aware datetime. Return UTC."""
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            raise InvalidInput(f"{name} must be an ISO-8601 datetime with a UTC offset") from None
    elif isinstance(value, datetime):
        moment = value
    else:
        raise InvalidInput(f"{name} must be an ISO-8601 datetime with a UTC offset")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvalidInput(f"{name} must include a UTC offset (for example +07:00)")
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    # Same fixed format as skeleton/log/activity.py, so string order is time order.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _as_local_day(day: str | date) -> date:
    if isinstance(day, datetime):
        raise InvalidInput("day must be a date or a YYYY-MM-DD string")
    if isinstance(day, date):
        return day
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        raise InvalidInput("day must be a date or a YYYY-MM-DD string") from None


def _utc_range(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tzinfo=JAKARTA)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=JAKARTA)
    return _iso(start), _iso(end)


def _as_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _check_status(status: Any, allowed: tuple[str, ...]) -> None:
    if status not in allowed:
        raise InvalidInput(f"status must be one of {list(allowed)}")


# ---------------------------------------------------------------- customers --


def add_customer(
    conn: sqlite3.Connection,
    business_id: int,
    name: str,
    phone: str | None = None,
    clock: Clock | None = None,
) -> int:
    """Insert a customer and return its id.

    ``phone`` is optional. ``clock`` sets ``created_at`` (default: system clock).
    """
    _require_id("business_id", business_id)
    name = _require_text("name", name)
    if phone is not None:
        phone = _require_text("phone", phone)
    created_at = _iso(_now(clock))
    with transaction(conn):
        if conn.execute("SELECT 1 FROM business WHERE id = ?", (business_id,)).fetchone() is None:
            raise UnknownBusinessError(f"business {business_id} not found")
        cur = conn.execute(
            "INSERT INTO customer (business_id, name, phone, created_at) VALUES (?, ?, ?, ?)",
            (business_id, name, phone, created_at),
        )
        return int(cur.lastrowid)


def _now(clock: Clock | None) -> datetime:
    if clock is None:
        return datetime.now(timezone.utc)
    return clock.now()


def get_customer(conn: sqlite3.Connection, business_id: int, customer_id: int) -> dict | None:
    """Return one customer of this business, or ``None``."""
    row = conn.execute(
        "SELECT * FROM customer WHERE id = ? AND business_id = ?", (customer_id, business_id)
    ).fetchone()
    return _as_dict(row) if row else None


def list_customers(conn: sqlite3.Connection, business_id: int) -> list[dict]:
    """All customers of this business, ordered by id."""
    rows = conn.execute("SELECT * FROM customer WHERE business_id = ? ORDER BY id", (business_id,))
    return [_as_dict(r) for r in rows]


# ----------------------------------------------------------------- bookings --


def request_booking(
    conn: sqlite3.Connection,
    business_id: int,
    customer_id: int,
    service: str,
    resource: str,
    starts_at: str | datetime,
    duration_min: int,
    clock: Clock,
) -> int:
    """Insert a booking with status ``requested`` and return its id.

    ``starts_at`` is an ISO-8601 string with a UTC offset (or an aware
    datetime); it is stored in UTC. ``duration_min`` is an int from 1 to 480.
    A request may overlap other bookings: only :func:`confirm_booking` checks.
    """
    _require_id("business_id", business_id)
    _require_id("customer_id", customer_id)
    service = _require_text("service", service)
    resource = _require_text("resource", resource)
    start = _parse_moment("starts_at", starts_at)
    _require_duration(duration_min)
    created_at = _iso(clock.now())
    with transaction(conn):
        if get_customer(conn, business_id, customer_id) is None:
            raise UnknownCustomerError(f"customer {customer_id} not found in this business")
        cur = conn.execute(
            "INSERT INTO booking (business_id, customer_id, service, resource, starts_at, "
            "duration_min, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?)",
            (business_id, customer_id, service, resource, _iso(start), duration_min, created_at),
        )
        return int(cur.lastrowid)


def get_booking(conn: sqlite3.Connection, business_id: int, booking_id: int) -> dict | None:
    """Return one booking of this business, or ``None``."""
    row = conn.execute(
        "SELECT * FROM booking WHERE id = ? AND business_id = ?", (booking_id, business_id)
    ).fetchone()
    return _as_dict(row) if row else None


def _confirmed_intervals(
    conn: sqlite3.Connection,
    business_id: int,
    resource: str,
    window_start: datetime,
    window_end: datetime,
    exclude_id: int | None = None,
) -> list[tuple[datetime, datetime]]:
    """Confirmed bookings on ``resource`` that may overlap ``[window_start, window_end)``."""
    # A booking lasts at most 8 hours, so one that starts earlier than that
    # before the window cannot reach into it. Exact overlap is decided by callers.
    rows = conn.execute(
        "SELECT starts_at, duration_min FROM booking "
        "WHERE business_id = ? AND resource = ? AND status = 'confirmed' "
        "AND starts_at > ? AND starts_at < ? AND id IS NOT ?",
        (business_id, resource, _iso(window_start - _MAX_DURATION), _iso(window_end), exclude_id),
    )
    intervals = []
    for row in rows:
        start = datetime.fromisoformat(row["starts_at"])
        intervals.append((start, start + timedelta(minutes=row["duration_min"])))
    return intervals


def _overlaps(start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in intervals)


def _booking_for_update(conn: sqlite3.Connection, business_id: int, booking_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM booking WHERE id = ? AND business_id = ?", (booking_id, business_id)
    ).fetchone()
    if row is None:
        raise UnknownBookingError(f"booking {booking_id} not found in this business")
    return row


def _set_booking_status(
    conn: sqlite3.Connection, booking_id: int, old: tuple[str, ...], new: str, row: sqlite3.Row
) -> None:
    if row["status"] not in old:
        raise BookingStateError(
            f"booking {booking_id} is {row['status']!r}; cannot change it to {new!r}"
        )
    cur = conn.execute(
        "UPDATE booking SET status = ? WHERE id = ? AND status = ?", (new, booking_id, row["status"])
    )
    if cur.rowcount != 1:  # defensive: the row was read in this transaction
        raise BookingStateError(f"booking {booking_id} could not be changed to {new!r}")


def confirm_booking(conn: sqlite3.Connection, business_id: int, booking_id: int) -> None:
    """Confirm a ``requested`` booking if its slot is free.

    Raises :class:`BookingConflict` when another confirmed booking on the same
    resource overlaps it; the booking then stays ``requested``. Raises
    :class:`BookingStateError` for any status other than ``requested``.
    """
    _require_id("business_id", business_id)
    _require_id("booking_id", booking_id)
    with transaction(conn):
        row = _booking_for_update(conn, business_id, booking_id)
        if row["status"] != "requested":
            raise BookingStateError(
                f"booking {booking_id} is {row['status']!r}; only 'requested' can be confirmed"
            )
        start = datetime.fromisoformat(row["starts_at"])
        end = start + timedelta(minutes=row["duration_min"])
        others = _confirmed_intervals(
            conn, business_id, row["resource"], start, end, exclude_id=booking_id
        )
        if _overlaps(start, end, others):
            raise BookingConflict(
                f"booking {booking_id} overlaps a confirmed booking on {row['resource']!r}"
            )
        _set_booking_status(conn, booking_id, ("requested",), "confirmed", row)


def reschedule_booking(
    conn: sqlite3.Connection,
    business_id: int,
    booking_id: int,
    new_start: str | datetime,
) -> None:
    """Move a ``requested`` booking to a new start time. It stays ``requested``.

    Used to re-request a booking at a proposed free slot. Like
    :func:`request_booking` it does not check overlaps; only
    :func:`confirm_booking` does. Raises :class:`BookingStateError` for any
    status other than ``requested``, so a confirmed booking never moves.
    """
    _require_id("business_id", business_id)
    _require_id("booking_id", booking_id)
    start = _parse_moment("new_start", new_start)
    with transaction(conn):
        row = _booking_for_update(conn, business_id, booking_id)
        if row["status"] != "requested":
            raise BookingStateError(
                f"booking {booking_id} is {row['status']!r}; only 'requested' can be rescheduled"
            )
        cur = conn.execute(
            "UPDATE booking SET starts_at = ? WHERE id = ? AND status = 'requested'",
            (_iso(start), booking_id),
        )
        if cur.rowcount != 1:  # defensive: the row was read in this transaction
            raise BookingStateError(f"booking {booking_id} could not be rescheduled")


def cancel_booking(conn: sqlite3.Connection, business_id: int, booking_id: int) -> None:
    """Cancel a ``requested`` or ``confirmed`` booking. A cancelled booking frees its slot."""
    _require_id("business_id", business_id)
    _require_id("booking_id", booking_id)
    with transaction(conn):
        row = _booking_for_update(conn, business_id, booking_id)
        _set_booking_status(conn, booking_id, ("requested", "confirmed"), "cancelled", row)


def complete_booking(conn: sqlite3.Connection, business_id: int, booking_id: int) -> None:
    """Mark a ``confirmed`` booking as ``done``."""
    _require_id("business_id", business_id)
    _require_id("booking_id", booking_id)
    with transaction(conn):
        row = _booking_for_update(conn, business_id, booking_id)
        _set_booking_status(conn, booking_id, ("confirmed",), "done", row)


def list_bookings(
    conn: sqlite3.Connection,
    business_id: int,
    day: str | date | None = None,
    status: str | None = None,
) -> list[dict]:
    """Bookings of this business ordered by start time, then id.

    ``day`` keeps bookings that start on that Asia/Jakarta calendar day.
    ``status`` keeps one status.
    """
    sql = "SELECT * FROM booking WHERE business_id = ?"
    params: list[Any] = [business_id]
    if day is not None:
        start, end = _utc_range(_as_local_day(day))
        sql += " AND starts_at >= ? AND starts_at < ?"
        params += [start, end]
    if status is not None:
        _check_status(status, BOOKING_STATUSES)
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY starts_at, id"
    return [_as_dict(r) for r in conn.execute(sql, params)]


def next_free_slot(
    conn: sqlite3.Connection,
    business_id: int,
    resource: str,
    duration_min: int,
    after: str | datetime,
    hours: tuple[int, int] = DEFAULT_HOURS,
) -> datetime | None:
    """Return the first free start time for ``resource``, or ``None``.

    Candidate starts lie on a 30-minute grid in Asia/Jakarta time (``:00`` and
    ``:30``), are not before ``after``, and the whole slot must fit inside the
    working ``hours`` ``(open, close)`` of one Jakarta day. Only *confirmed*
    bookings occupy a slot (same rule as :func:`confirm_booking`); a slot that
    ends exactly when another booking starts is free. The search covers
    ``after``'s day and the next :data:`MAX_SEARCH_DAYS` days. The result is an
    aware datetime in Asia/Jakarta, which :func:`request_booking` accepts.
    """
    resource = _require_text("resource", resource)
    _require_duration(duration_min)
    after_utc = _parse_moment("after", after)
    if (
        not isinstance(hours, (tuple, list))
        or len(hours) != 2
        or not all(_is_int(h) for h in hours)
        or not 0 <= hours[0] < hours[1] <= 24
    ):
        raise InvalidInput("hours must be (open, close) ints with 0 <= open < close <= 24")
    open_hour, close_hour = hours
    duration = timedelta(minutes=duration_min)

    first_day = after_utc.astimezone(JAKARTA).date()
    horizon = datetime.combine(
        first_day + timedelta(days=MAX_SEARCH_DAYS + 1), time.min, tzinfo=JAKARTA
    ).astimezone(timezone.utc)
    busy = _confirmed_intervals(conn, business_id, resource, after_utc, horizon)

    for offset in range(MAX_SEARCH_DAYS + 1):
        midnight = datetime.combine(
            first_day + timedelta(days=offset), time.min, tzinfo=JAKARTA
        ).astimezone(timezone.utc)
        opens = midnight + timedelta(hours=open_hour)
        closes = midnight + timedelta(hours=close_hour)
        start = opens
        if after_utc > opens:
            start = opens + SLOT_STEP * -(-(after_utc - opens) // SLOT_STEP)
        while start + duration <= closes:
            if not _overlaps(start, start + duration, busy):
                return start.astimezone(JAKARTA)
            start += SLOT_STEP
    return None


# ----------------------------------------------------------------- invoices --


def create_invoice(
    conn: sqlite3.Connection,
    business_id: int,
    customer_id: int,
    description: str,
    amount_idr: int,
    booking_id: int | None = None,
    clock: Clock | None = None,
) -> int:
    """Insert an invoice with status ``draft`` and return its id.

    The customer, and the booking when given, must belong to the business.
    ``clock`` sets ``created_at`` (default: system clock).
    """
    _require_id("business_id", business_id)
    _require_id("customer_id", customer_id)
    description = _require_text("description", description)
    _require_amount("amount_idr", amount_idr)
    if booking_id is not None:
        _require_id("booking_id", booking_id)
    created_at = _iso(_now(clock))
    with transaction(conn):
        if get_customer(conn, business_id, customer_id) is None:
            raise UnknownCustomerError(f"customer {customer_id} not found in this business")
        if booking_id is not None and get_booking(conn, business_id, booking_id) is None:
            raise UnknownBookingError(f"booking {booking_id} not found in this business")
        cur = conn.execute(
            "INSERT INTO invoice (business_id, customer_id, booking_id, description, amount_idr, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
            (business_id, customer_id, booking_id, description, amount_idr, created_at),
        )
        return int(cur.lastrowid)


def get_invoice(conn: sqlite3.Connection, business_id: int, invoice_id: int) -> dict | None:
    """Return one invoice of this business, or ``None``."""
    row = conn.execute(
        "SELECT * FROM invoice WHERE id = ? AND business_id = ?", (invoice_id, business_id)
    ).fetchone()
    return _as_dict(row) if row else None


def _invoice_for_update(conn: sqlite3.Connection, business_id: int, invoice_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM invoice WHERE id = ? AND business_id = ?", (invoice_id, business_id)
    ).fetchone()
    if row is None:
        raise UnknownInvoiceError(f"invoice {invoice_id} not found in this business")
    return row


def _move_invoice(
    conn: sqlite3.Connection,
    invoice_id: int,
    row: sqlite3.Row,
    allowed_from: tuple[str, ...],
    new_status: str,
    extra_set: str = "",
    extra_args: tuple = (),
) -> None:
    if row["status"] not in allowed_from:
        raise InvoiceStateError(
            f"invoice {invoice_id} is {row['status']!r}; cannot change it to {new_status!r}"
        )
    cur = conn.execute(
        f"UPDATE invoice SET status = ?{extra_set} WHERE id = ? AND status = ?",
        (new_status, *extra_args, invoice_id, row["status"]),
    )
    if cur.rowcount != 1:  # defensive: the row was read in this transaction
        raise InvoiceStateError(f"invoice {invoice_id} could not be changed to {new_status!r}")


def approve_invoice(
    conn: sqlite3.Connection,
    business_id: int,
    invoice_id: int,
    approved_by: str,
    clock: Clock,
) -> None:
    """Approve a ``draft`` invoice. ``approved_by`` is ``'owner'`` or ``'policy'``.

    After this the invoice can no longer be edited (database triggers).
    """
    _require_id("business_id", business_id)
    _require_id("invoice_id", invoice_id)
    if approved_by not in APPROVERS:
        raise InvalidInput(f"approved_by must be one of {list(APPROVERS)}")
    approved_at = _iso(clock.now())
    with transaction(conn):
        row = _invoice_for_update(conn, business_id, invoice_id)
        _move_invoice(
            conn, invoice_id, row, ("draft",), "approved",
            ", approved_by = ?, approved_at = ?", (approved_by, approved_at),
        )


def mark_paid(conn: sqlite3.Connection, business_id: int, invoice_id: int, clock: Clock) -> None:
    """Mark an ``approved`` invoice as ``paid`` and set ``paid_at``."""
    _require_id("business_id", business_id)
    _require_id("invoice_id", invoice_id)
    paid_at = _iso(clock.now())
    with transaction(conn):
        row = _invoice_for_update(conn, business_id, invoice_id)
        _move_invoice(conn, invoice_id, row, ("approved",), "paid", ", paid_at = ?", (paid_at,))


def void_invoice(conn: sqlite3.Connection, business_id: int, invoice_id: int) -> None:
    """Void a ``draft`` or ``approved`` invoice. Paid and void invoices cannot be voided."""
    _require_id("business_id", business_id)
    _require_id("invoice_id", invoice_id)
    with transaction(conn):
        row = _invoice_for_update(conn, business_id, invoice_id)
        _move_invoice(conn, invoice_id, row, ("draft", "approved"), "void")


def list_invoices(
    conn: sqlite3.Connection, business_id: int, status: str | None = None
) -> list[dict]:
    """Invoices of this business ordered by id, optionally one status."""
    sql = "SELECT * FROM invoice WHERE business_id = ?"
    params: list[Any] = [business_id]
    if status is not None:
        _check_status(status, INVOICE_STATUSES)
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id"
    return [_as_dict(r) for r in conn.execute(sql, params)]


def unpaid_overdue(
    conn: sqlite3.Connection, business_id: int, clock: Clock, days: int = 7
) -> list[dict]:
    """Approved invoices not paid more than ``days`` days after approval.

    An invoice approved exactly ``days`` days ago is not yet overdue. Oldest
    approval first.
    """
    if not _is_int(days) or days < 0:
        raise InvalidInput("days must be a non-negative int")
    cutoff = _iso(clock.now() - timedelta(days=days))
    rows = conn.execute(
        "SELECT * FROM invoice WHERE business_id = ? AND status = 'approved' AND approved_at < ? "
        "ORDER BY approved_at, id",
        (business_id, cutoff),
    )
    return [_as_dict(r) for r in rows]


def revenue_for_day(conn: sqlite3.Connection, business_id: int, day: str | date) -> int:
    """Sum of ``amount_idr`` of paid invoices whose ``paid_at`` is on this Jakarta day."""
    start, end = _utc_range(_as_local_day(day))
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_idr), 0) FROM invoice "
        "WHERE business_id = ? AND status = 'paid' AND paid_at >= ? AND paid_at < ?",
        (business_id, start, end),
    ).fetchone()
    return int(row[0])
