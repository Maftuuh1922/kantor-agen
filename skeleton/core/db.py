"""SQLite access layer for Kantor Agen.

Money is stored as integer rupiah (columns end in ``_idr``). Transactions are
explicit: connections use ``isolation_level=None`` and callers wrap writes in
:func:`transaction`. This module must not import anything from ``apps/``.
"""

from __future__ import annotations

import itertools
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

BUSY_TIMEOUT_SECONDS = 5.0
BEGIN_RETRY_BACKOFF = (0.05, 0.1, 0.2)  # seconds; one sleep per retry

SCHEMA = """
CREATE TABLE IF NOT EXISTS business (
    id      INTEGER PRIMARY KEY,
    name    TEXT    NOT NULL,
    type    TEXT    NOT NULL,
    paused  INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))
);

CREATE TABLE IF NOT EXISTS agent (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    role         TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    desk_index   INTEGER NOT NULL CHECK (desk_index >= 0),
    paused       INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))
);

CREATE TABLE IF NOT EXISTS policy (
    business_id      INTEGER PRIMARY KEY REFERENCES business(id) ON DELETE CASCADE,
    daily_limit_idr  INTEGER NOT NULL DEFAULT 100000 CHECK (daily_limit_idr >= 0),
    blocked_actions  TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS spend_ledger (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    agent_id     INTEGER REFERENCES agent(id) ON DELETE SET NULL,
    amount_idr   INTEGER NOT NULL CHECK (amount_idr >= 0),
    day          TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_ledger_business_day
    ON spend_ledger(business_id, day);

CREATE TABLE IF NOT EXISTS product (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    qty             INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    min_qty         INTEGER NOT NULL DEFAULT 0 CHECK (min_qty >= 0),
    unit_price_idr  INTEGER NOT NULL CHECK (unit_price_idr >= 0),
    unit_cost_idr   INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost_idr >= 0)
);

CREATE TABLE IF NOT EXISTS sale (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    total_idr    INTEGER NOT NULL CHECK (total_idr >= 0)
);

CREATE TABLE IF NOT EXISTS sale_item (
    id              INTEGER PRIMARY KEY,
    sale_id         INTEGER NOT NULL REFERENCES sale(id) ON DELETE CASCADE,
    product_id      INTEGER NOT NULL REFERENCES product(id),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    unit_price_idr  INTEGER NOT NULL CHECK (unit_price_idr >= 0),
    discount_idr    INTEGER NOT NULL DEFAULT 0 CHECK (discount_idr >= 0)
);

CREATE TABLE IF NOT EXISTS purchase_order (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES product(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    cost_idr    INTEGER NOT NULL CHECK (cost_idr >= 0),
    status      TEXT    NOT NULL CHECK (status IN ('placed', 'pending', 'received', 'cancelled')),
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    agent_id     INTEGER REFERENCES agent(id) ON DELETE SET NULL,
    action       TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    cost_idr     INTEGER NOT NULL DEFAULT 0 CHECK (cost_idr >= 0),
    status       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    agent_id     INTEGER REFERENCES agent(id) ON DELETE SET NULL,
    channel      TEXT    NOT NULL,
    recipient    TEXT    NOT NULL,
    body         TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status = 'pending')
);
"""


# One request that an agent cannot decide alone: the owner approves or rejects it (task 14.2).
# The skeleton knows no business type: ``kind`` is a word the app chooses ("purchase_order", "invoice") and ``ref``
# identifies the thing asked about ("product:7"), so the same thing is never asked twice at once. ``detail`` is JSON
# the app writes. Decisions are made only through ``skeleton.policy.approvals.decide``.
APPROVAL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS approval (
        id            INTEGER PRIMARY KEY,
        business_id   INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
        agent_id      INTEGER REFERENCES agent(id) ON DELETE SET NULL,
        kind          TEXT    NOT NULL CHECK (length(trim(kind)) > 0),
        ref           TEXT    NOT NULL CHECK (length(trim(ref)) > 0),
        subject       TEXT    NOT NULL CHECK (length(trim(subject)) > 0),
        detail        TEXT    NOT NULL DEFAULT '{}',
        amount_idr    INTEGER NOT NULL DEFAULT 0 CHECK (typeof(amount_idr) = 'integer' AND amount_idr >= 0),
        status        TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected', 'failed')),
        created_at    TEXT    NOT NULL,
        decided_at    TEXT,
        decided_by    TEXT    CHECK (decided_by IS NULL OR decided_by = 'owner'),
        decision_note TEXT,
        CHECK ((status = 'pending') = (decided_at IS NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_approval_business_status ON approval(business_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_approval_ref ON approval(business_id, kind, ref, status)",
    # At most one open request per thing: the database itself makes request_approval idempotent.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_one_pending "
    "ON approval(business_id, kind, ref) WHERE status = 'pending'",
)

_PURCHASE_ORDER_TABLE = """
CREATE TABLE purchase_order{suffix} (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES product(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    cost_idr    INTEGER NOT NULL CHECK (cost_idr >= 0),
    status      TEXT    NOT NULL CHECK (status IN ('placed', 'pending', 'received', 'cancelled')),
    received_at TEXT
)
"""


def connect(path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with the settings the rest of the app relies on.

    ``check_same_thread=False`` lets the connection be used from a thread other
    than the one that created it. The web app needs this because the connection
    is created at startup but used from the server's event loop thread. It is
    only safe when the caller never uses the connection from two threads at the
    same time (the office API runs every handler on the event loop thread).
    """
    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Safe to call repeatedly."""
    # executescript issues its own COMMIT first; with isolation_level=None this
    # is fine because no transaction is open.
    conn.executescript(SCHEMA)
    upgrade_schema(conn)


def ensure_approval_table(conn: sqlite3.Connection) -> None:
    """Create the ``approval`` table and its indexes when missing. Idempotent.

    ``init_schema`` already does this for every database it touches (it is also what makes an old database file
    get the table). This function exists for startup code that holds a connection but not the schema script.
    Each statement runs through ``conn.execute``, so an open transaction is not committed.
    """
    for statement in APPROVAL_STATEMENTS:
        conn.execute(statement)


def ensure_purchase_order_cancelled(conn: sqlite3.Connection) -> None:
    """Let an existing ``purchase_order`` table hold the status ``cancelled``. Idempotent.

    SQLite cannot change a CHECK constraint, so a database file made before task 14.2 (whose table allows only
    ``placed``, ``pending`` and ``received``) gets its table rebuilt once: same columns, same ids, same rows, new
    constraint. Nothing references ``purchase_order``, so no foreign key is affected. New databases already have
    the wider constraint and are left alone.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'purchase_order'").fetchone()
    if row is None or row[0] is None or "'cancelled'" in row[0]:
        return
    with transaction(conn):
        conn.execute(_PURCHASE_ORDER_TABLE.format(suffix="_new"))
        conn.execute(
            "INSERT INTO purchase_order_new (id, product_id, qty, cost_idr, status, received_at) "
            "SELECT id, product_id, qty, cost_idr, status, received_at FROM purchase_order"
        )
        conn.execute("DROP TABLE purchase_order")
        conn.execute("ALTER TABLE purchase_order_new RENAME TO purchase_order")


def upgrade_schema(conn: sqlite3.Connection) -> None:
    """Bring a database file made by an older version up to date. Idempotent; ``init_schema`` calls it."""
    ensure_approval_table(conn)
    ensure_purchase_order_cancelled(conn)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _begin(conn: sqlite3.Connection, statement: str) -> None:
    """Run BEGIN, retrying on lock contention with short backoff."""
    for delay in BEGIN_RETRY_BACKOFF:
        try:
            conn.execute(statement)
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            time.sleep(delay)
    conn.execute(statement)  # final attempt; any error propagates


_savepoint_ids = itertools.count(1)


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Explicit transaction: commit on success, roll back on any exception.

    ``immediate=True`` uses BEGIN IMMEDIATE so the write lock is taken up front.
    Starting the transaction is retried up to 3 times on "locked"/"busy" errors
    (sleeping 0.05s, 0.1s, 0.2s), then the error is re-raised.

    Called while a transaction is already open on ``conn``, it becomes a SAVEPOINT instead of failing: its own
    writes commit or roll back with the outer transaction, and an exception inside it undoes only its own writes
    before the exception moves on. This is what lets the owner's decision (``skeleton.policy.approvals.decide``)
    run an app's handler, the policy check and the activity row as one all-or-nothing unit.
    """
    if conn.in_transaction:
        name = f"kantor_sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    _begin(conn, "BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
