"""Jasa tables and the database triggers that protect approved invoices.

The skeleton schema (``skeleton.core.db``) knows nothing about bookings or
invoices, so the Jasa app creates its own tables here. Call
:func:`init_jasa_schema` once per database after ``db.init_schema(conn)``. It is
idempotent and never touches skeleton tables.

Every statement runs through ``conn.execute`` (not ``executescript``), so the
call does not commit a transaction the caller may have open.

Timestamps are UTC ISO-8601 strings with microseconds and a ``+00:00`` offset,
the same format as the skeleton, so string order is time order.

Invoice rules enforced by the database itself (Req 5.1, 6.2: an invoice must
not change after the owner or policy approves it):

* Allowed status changes: ``draft -> approved``, ``draft -> void``,
  ``approved -> paid``, ``approved -> void``. Any other change aborts.
* Once an invoice is ``approved``, ``paid`` or ``void``, its business, customer,
  booking, description, amount, approval fields and creation time cannot
  change, and ``paid_at`` cannot change once the invoice is paid or void.
* While a draft leaves ``draft``, only ``status`` and the approval fields may
  change in the same statement (no editing the amount while approving).
* Only ``draft`` invoices can be deleted.

This module imports nothing from ``skeleton``, and the skeleton never imports
this module.
"""

from __future__ import annotations

import sqlite3

BOOKING_STATUSES = ("requested", "confirmed", "cancelled", "done")
INVOICE_STATUSES = ("draft", "approved", "paid", "void")
APPROVERS = ("owner", "policy")
MAX_DURATION_MIN = 480  # 8 hours

_STATUS_LIST = ", ".join(f"'{s}'" for s in BOOKING_STATUSES)
_INVOICE_STATUS_LIST = ", ".join(f"'{s}'" for s in INVOICE_STATUSES)
_APPROVER_LIST = ", ".join(f"'{a}'" for a in APPROVERS)

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS customer (
        id           INTEGER PRIMARY KEY,
        business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
        name         TEXT    NOT NULL CHECK (length(trim(name)) > 0),
        phone        TEXT,
        created_at   TEXT    NOT NULL,
        UNIQUE (id, business_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS booking (
        id            INTEGER PRIMARY KEY,
        business_id   INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
        customer_id   INTEGER NOT NULL,
        service       TEXT    NOT NULL CHECK (length(trim(service)) > 0),
        resource      TEXT    NOT NULL CHECK (length(trim(resource)) > 0),
        starts_at     TEXT    NOT NULL,
        duration_min  INTEGER NOT NULL CHECK (duration_min BETWEEN 1 AND {MAX_DURATION_MIN}),
        status        TEXT    NOT NULL CHECK (status IN ({_STATUS_LIST})),
        created_at    TEXT    NOT NULL,
        UNIQUE (id, business_id),
        FOREIGN KEY (customer_id, business_id) REFERENCES customer(id, business_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS invoice (
        id           INTEGER PRIMARY KEY,
        business_id  INTEGER NOT NULL REFERENCES business(id),
        customer_id  INTEGER NOT NULL,
        booking_id   INTEGER,
        description  TEXT    NOT NULL CHECK (length(trim(description)) > 0),
        amount_idr   INTEGER NOT NULL CHECK (typeof(amount_idr) = 'integer' AND amount_idr >= 0),
        status       TEXT    NOT NULL CHECK (status IN ({_INVOICE_STATUS_LIST})),
        approved_by  TEXT    CHECK (approved_by IN ({_APPROVER_LIST})),
        approved_at  TEXT,
        paid_at      TEXT,
        created_at   TEXT    NOT NULL,
        FOREIGN KEY (customer_id, business_id) REFERENCES customer(id, business_id),
        FOREIGN KEY (booking_id, business_id) REFERENCES booking(id, business_id),
        CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
        CHECK (status <> 'draft' OR approved_by IS NULL),
        CHECK (status NOT IN ('approved', 'paid') OR approved_by IS NOT NULL),
        CHECK ((status = 'paid') = (paid_at IS NOT NULL))
    )
    """,
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_customer_business ON customer(business_id)",
    "CREATE INDEX IF NOT EXISTS idx_booking_resource_start "
    "ON booking(business_id, resource, starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_booking_status_start "
    "ON booking(business_id, status, starts_at)",
    "CREATE INDEX IF NOT EXISTS idx_booking_customer ON booking(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_business_status "
    "ON invoice(business_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_paid_at ON invoice(business_id, paid_at)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_customer ON invoice(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_booking ON invoice(booking_id)",
)

_CORE_CHANGED = (
    "NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id "
    "OR NEW.customer_id IS NOT OLD.customer_id OR NEW.booking_id IS NOT OLD.booking_id "
    "OR NEW.description IS NOT OLD.description OR NEW.amount_idr IS NOT OLD.amount_idr "
    "OR NEW.created_at IS NOT OLD.created_at"
)

_INVOICE_TRIGGERS = (
    f"""
    CREATE TRIGGER IF NOT EXISTS invoice_immutable_after_approval
    BEFORE UPDATE ON invoice
    WHEN OLD.status IN ('approved', 'paid', 'void') AND (
        {_CORE_CHANGED}
        OR NEW.approved_by IS NOT OLD.approved_by
        OR NEW.approved_at IS NOT OLD.approved_at
        OR (OLD.status IN ('paid', 'void') AND NEW.paid_at IS NOT OLD.paid_at)
    )
    BEGIN
        SELECT RAISE(ABORT, 'invoice is immutable after approval');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS invoice_draft_leaves_unedited
    BEFORE UPDATE ON invoice
    WHEN OLD.status = 'draft' AND NEW.status <> 'draft' AND (
        {_CORE_CHANGED}
        OR (NEW.status = 'void'
            AND (NEW.approved_by IS NOT NULL OR NEW.approved_at IS NOT NULL))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invoice cannot be edited while it changes status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS invoice_status_transition
    BEFORE UPDATE OF status ON invoice
    WHEN NEW.status IS NOT OLD.status AND NOT (
        (OLD.status = 'draft' AND NEW.status IN ('approved', 'void'))
        OR (OLD.status = 'approved' AND NEW.status IN ('paid', 'void'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid invoice status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS invoice_no_delete_after_draft
    BEFORE DELETE ON invoice
    WHEN OLD.status <> 'draft'
    BEGIN
        SELECT RAISE(ABORT, 'only draft invoices can be deleted');
    END
    """,
)


def install_invoice_triggers(conn: sqlite3.Connection) -> None:
    """Install the invoice immutability triggers. Safe to call repeatedly.

    The ``invoice`` table must exist; :func:`init_jasa_schema` calls this after
    creating it.
    """
    for statement in _INVOICE_TRIGGERS:
        conn.execute(statement)


def init_jasa_schema(conn: sqlite3.Connection) -> None:
    """Create the Jasa tables, indexes and invoice triggers. Idempotent.

    Needs the skeleton ``business`` table (call ``db.init_schema(conn)`` first).
    Does not change any skeleton table.
    """
    for statement in _TABLES:
        conn.execute(statement)
    for statement in _INDEXES:
        conn.execute(statement)
    install_invoice_triggers(conn)
