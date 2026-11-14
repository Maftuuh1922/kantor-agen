import sqlite3

import pytest

from skeleton.core import db

EXPECTED_TABLES = {
    "business", "agent", "policy", "spend_ledger", "product", "sale",
    "sale_item", "purchase_order", "activity", "outbox",
}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    yield c
    c.close()


def _add_product(conn, qty=5):
    conn.execute(
        "INSERT INTO product (business_id, name, qty, unit_price_idr) VALUES (1, 'Teh', ?, 3000)",
        (qty,),
    )


def _count(conn, table="product"):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_schema_creates_all_tables(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names
    idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_spend_ledger_business_day" in idx


def test_connect_settings(conn, tmp_path):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.row_factory is sqlite3.Row
    assert conn.isolation_level is None
    file_conn = db.connect(str(tmp_path / "t.db"))
    try:
        assert file_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        file_conn.close()


def test_init_schema_is_idempotent(conn):
    db.init_schema(conn)


def test_negative_qty_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _add_product(conn, qty=-1)


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent (business_id, role, name, desk_index) VALUES (99, 'r', 'n', 0)")


def test_purchase_order_status_check(conn):
    _add_product(conn)
    conn.execute("INSERT INTO purchase_order (product_id, qty, cost_idr, status) VALUES (1, 1, 100, 'placed')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO purchase_order (product_id, qty, cost_idr, status) VALUES (1, 1, 100, 'shipped')")


def test_outbox_only_accepts_pending(conn):
    conn.execute(
        "INSERT INTO outbox (business_id, channel, recipient, body) VALUES (1, 'wa', 'x', 'hi')"
    )
    assert conn.execute("SELECT status FROM outbox").fetchone()["status"] == "pending"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outbox (business_id, channel, recipient, body, status) "
            "VALUES (1, 'wa', 'x', 'hi', 'sent')"
        )


def test_negative_amounts_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO spend_ledger (business_id, amount_idr, day, created_at) "
            "VALUES (1, -5, '2024-01-01', 'now')"
        )


def test_transaction_commits(conn):
    with db.transaction(conn):
        _add_product(conn)
    assert _count(conn) == 1
    assert not conn.in_transaction


def test_transaction_rolls_back_on_exception(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            _add_product(conn)
            raise RuntimeError("boom")
    assert _count(conn) == 0
    assert not conn.in_transaction


def test_transaction_rolls_back_on_constraint_error(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            _add_product(conn, qty=2)
            _add_product(conn, qty=-1)
    assert _count(conn) == 0


class FakeConn:
    """Fails BEGIN with a lock error a set number of times."""

    def __init__(self, failures, message="database is locked"):
        self.failures = failures
        self.message = message
        self.calls = []
        self.in_transaction = False

    def execute(self, sql):
        self.calls.append(sql)
        if sql.startswith("BEGIN"):
            if self.failures > 0:
                self.failures -= 1
                raise sqlite3.OperationalError(self.message)
            self.in_transaction = True
        else:
            self.in_transaction = False


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(db.time, "sleep", recorded.append)
    return recorded


def test_begin_gives_up_after_three_retries(sleeps):
    fake = FakeConn(failures=100)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with db.transaction(fake):
            pytest.fail("body must not run")
    assert sleeps == [0.05, 0.1, 0.2]
    assert fake.calls == ["BEGIN IMMEDIATE"] * 4


def test_begin_succeeds_after_retries(sleeps):
    fake = FakeConn(failures=2, message="database is busy")
    with db.transaction(fake):
        pass
    assert sleeps == [0.05, 0.1]
    assert fake.calls[-1] == "COMMIT"


def test_begin_does_not_retry_other_errors(sleeps):
    fake = FakeConn(failures=1, message="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk"):
        with db.transaction(fake):
            pass
    assert sleeps == []


def test_deferred_transaction(conn):
    with db.transaction(conn, immediate=False):
        _add_product(conn)
    assert _count(conn) == 1
