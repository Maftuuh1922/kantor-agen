"""SQLite access layer for Kantor Agen.

Money is stored as integer rupiah (columns end in ``_idr``). Transactions are
explicit: connections use ``isolation_level=None`` and callers wrap writes in
:func:`transaction`. This module must not import anything from ``apps/``.
"""

from __future__ import annotations

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
    status      TEXT    NOT NULL CHECK (status IN ('placed', 'pending', 'received')),
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


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Explicit transaction: commit on success, roll back on any exception.

    ``immediate=True`` uses BEGIN IMMEDIATE so the write lock is taken up front.
    Starting the transaction is retried up to 3 times on "locked"/"busy" errors
    (sleeping 0.05s, 0.1s, 0.2s), then the error is re-raised.
    """
    _begin(conn, "BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
