"""Unit tests for owner approvals in the skeleton (task 14.2; Req 5.1, 5.2, 7.1, 8.2).

The skeleton knows no business type, so these tests use a small generic handler that spends money through
``authorize_spend``, like the Warung one does. Real schema in memory SQLite, FakeClock, no network.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.events import EventBus
from skeleton.log.activity import install_append_only_triggers, recent
from skeleton.policy import (
    ApprovalBlocked,
    ApprovalFailed,
    ApprovalHandlers,
    ApprovalNotFound,
    ApprovalNotPending,
    HandlerOutcome,
    authorize_spend,
    decide,
    list_pending,
    rejected_recently,
    remaining_today,
    request_approval,
    set_policy,
    spent_today,
)
from skeleton.policy import approvals as approvals_module

KIND = "thing"


class SpendHandler:
    """Generic handler: approving spends the amount through the policy engine; rejecting costs nothing."""

    def __init__(self):
        self.approved = []
        self.rejected = []

    def approve(self, conn, approval, clock):
        authorize_spend(conn, approval["business_id"], approval["agent_id"], approval["amount_idr"], clock)
        self.approved.append(approval["id"])
        return HandlerOutcome(message="Barang dipesan", cost_idr=approval["amount_idr"])

    def reject(self, conn, approval, clock):
        self.rejected.append(approval["id"])
        return HandlerOutcome(message="Barang dibatalkan")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    install_append_only_triggers(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Lain', 'warung')")
    c.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (1, 1, 'stock', 'Budi', 0)")
    set_policy(c, 1, 100_000, [])
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


@pytest.fixture
def handler():
    return SpendHandler()


@pytest.fixture
def handlers(handler):
    registry = ApprovalHandlers()
    registry.register(KIND, handler)
    return registry


class RecordingBus:
    """Duck-typed bus: keeps every published event, so a test can look at them without an event loop."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(dict(event))


def ask(conn, clock, amount=60_000, ref="a", subject="Pesan stok Teh", **kw):
    return request_approval(conn, 1, 1, KIND, subject, {"summary": "20 pcs", "reason": "stok menipis"}, amount, clock, ref=ref, **kw)


def ledger(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM spend_ledger ORDER BY id")]


def activity(conn):
    return recent(conn, 1, limit=100)


# ---------------------------------------------------------------- schema --
def test_init_schema_creates_the_approval_table_and_is_idempotent(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"approval", "idx_approval_one_pending"} <= names
    db.init_schema(conn)
    db.ensure_approval_table(conn)
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(approval)")]
    for column in ("id", "business_id", "agent_id", "kind", "subject", "detail", "amount_idr", "status",
                   "created_at", "decided_at", "decided_by", "decision_note"):
        assert column in columns


def test_approval_table_constraints(conn, clock):
    approval_id = ask(conn, clock)
    with pytest.raises(sqlite3.IntegrityError):  # unknown status
        conn.execute("UPDATE approval SET status = 'maybe', decided_at = 'x' WHERE id = ?", (approval_id,))
    with pytest.raises(sqlite3.IntegrityError):  # negative amount
        conn.execute(
            "INSERT INTO approval (business_id, kind, ref, subject, amount_idr, created_at) "
            "VALUES (1, 'k', 'r', 's', -1, 'now')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # only the owner decides
        conn.execute("UPDATE approval SET status = 'approved', decided_at = 'x', decided_by = 'agent'")


def test_an_old_database_file_gets_the_table_and_the_cancelled_status(tmp_path):
    """A file made before task 14.2: no approval table, purchase_order allows only three statuses."""
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(
        """
        CREATE TABLE business (id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                               paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)));
        CREATE TABLE product (id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, name TEXT NOT NULL,
                              qty INTEGER NOT NULL DEFAULT 0, min_qty INTEGER NOT NULL DEFAULT 0,
                              unit_price_idr INTEGER NOT NULL, unit_cost_idr INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE purchase_order (
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES product(id),
            qty INTEGER NOT NULL CHECK (qty > 0), cost_idr INTEGER NOT NULL CHECK (cost_idr >= 0),
            status TEXT NOT NULL CHECK (status IN ('placed', 'pending', 'received')), received_at TEXT);
        INSERT INTO business VALUES (1, 'Warung', 'warung', 0);
        INSERT INTO product VALUES (1, 1, 'Teh', 1, 5, 3000, 2000);
        INSERT INTO purchase_order VALUES (7, 1, 10, 20000, 'pending', NULL);
        INSERT INTO purchase_order VALUES (8, 1, 5, 10000, 'received', '2025-01-01T00:00:00.000000+00:00');
        """
    )
    old.close()
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        db.init_schema(conn)  # a second start changes nothing
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'approval'").fetchone()
        rows = [tuple(r) for r in conn.execute("SELECT id, product_id, qty, cost_idr, status, received_at FROM purchase_order ORDER BY id")]
        assert rows == [(7, 1, 10, 20000, "pending", None), (8, 1, 5, 10000, "received", "2025-01-01T00:00:00.000000+00:00")]
        conn.execute("UPDATE purchase_order SET status = 'cancelled' WHERE id = 7")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE purchase_order SET status = 'shipped' WHERE id = 7")
    finally:
        conn.close()


def test_transaction_nests_as_a_savepoint(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO product (business_id, name, unit_price_idr) VALUES (1, 'a', 1)")
            with pytest.raises(ValueError):
                with db.transaction(conn):
                    conn.execute("INSERT INTO product (business_id, name, unit_price_idr) VALUES (1, 'b', 1)")
                    raise ValueError("inner")
            assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1  # only the inner write is undone
            raise RuntimeError("outer")
    assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 0
    assert not conn.in_transaction


# ------------------------------------------------------------ requesting --
def test_request_is_idempotent_per_kind_and_ref(conn, clock):
    first = ask(conn, clock, ref="product:1")
    again = ask(conn, clock, ref="product:1", subject="another wording")
    other = ask(conn, clock, ref="product:2")
    assert first == again != other
    assert [a["id"] for a in list_pending(conn, 1)] == [first, other]
    one = list_pending(conn, 1)[0]
    assert (one["kind"], one["status"], one["agent_id"], one["amount_idr"]) == (KIND, "pending", 1, 60_000)
    assert one["detail"] == {"summary": "20 pcs", "reason": "stok menipis"}
    assert one["decided_at"] is None and one["decided_by"] is None


def test_request_validation(conn, clock):
    for bad in (-1, True, 1.5, "5"):
        with pytest.raises(ValueError):
            ask(conn, clock, amount=bad)
    with pytest.raises(ValueError):
        request_approval(conn, 1, 1, "", "s", {}, 1, clock)
    with pytest.raises(ValueError):
        request_approval(conn, 1, 1, KIND, "  ", {}, 1, clock)
    assert list_pending(conn, 1) == []


def test_request_publishes_once_and_not_for_a_repeat(conn, clock):
    bus = RecordingBus()
    first = ask(conn, clock, bus=bus)
    ask(conn, clock, bus=bus)
    assert [(e["type"], e["approval_id"], e["status"], e["pending_count"]) for e in bus.events] == [
        ("approval_changed", first, "pending", 1)
    ]


def test_request_works_with_a_real_event_bus(conn, clock):
    bus = EventBus()
    sub = bus.subscribe()
    approval_id = ask(conn, clock, bus=bus)
    event = asyncio.run(asyncio.wait_for(sub.get(), 1))
    assert event["type"] == "approval_changed" and event["approval_id"] == approval_id


# --------------------------------------------------------------- deciding --
def test_approve_spends_through_the_ledger_and_logs_the_decision(conn, clock, handlers, handler):
    approval_id = ask(conn, clock, amount=60_000)
    result = decide(conn, 1, approval_id, True, handlers, clock, note="oke")
    assert (result.status, result.cost_idr, result.ok) == ("approved", 60_000, True)
    assert handler.approved == [approval_id]
    assert [r["amount_idr"] for r in ledger(conn)] == [60_000]
    assert spent_today(conn, 1, clock) == 60_000
    row = approvals_module.get(conn, 1, approval_id)
    assert (row["status"], row["decided_by"], row["decision_note"]) == ("approved", "owner", "oke")
    assert row["decided_at"] is not None
    (entry,) = activity(conn)
    assert (entry["action"], entry["agent_id"], entry["cost_idr"], entry["status"]) == ("Keputusan pemilik", 1, 60_000, "done")
    assert "menyetujui" in entry["reason"] and "Pesan stok Teh" in entry["reason"] and "Rp 60.000" in entry["reason"]
    assert "oke" in entry["reason"]
    assert list_pending(conn, 1) == []


def test_reject_writes_no_ledger_row_and_logs_the_decision(conn, clock, handlers, handler):
    approval_id = ask(conn, clock)
    result = decide(conn, 1, approval_id, False, handlers, clock)
    assert (result.status, result.cost_idr) == ("rejected", 0)
    assert handler.rejected == [approval_id] and handler.approved == []
    assert ledger(conn) == []
    (entry,) = activity(conn)
    assert (entry["action"], entry["cost_idr"], entry["status"]) == ("Keputusan pemilik", 0, "rejected")
    assert "menolak" in entry["reason"]


def test_only_a_pending_request_can_be_decided_once(conn, clock, handlers):
    approval_id = ask(conn, clock)
    decide(conn, 1, approval_id, True, handlers, clock)
    before = (ledger(conn), activity(conn))
    for approve in (True, False):
        with pytest.raises(ApprovalNotPending) as caught:
            decide(conn, 1, approval_id, approve, handlers, clock)
        assert caught.value.status == "approved"
    assert (ledger(conn), activity(conn)) == before  # the second decision wrote nothing


def test_unknown_id_and_other_business_are_not_found(conn, clock, handlers):
    approval_id = ask(conn, clock)
    with pytest.raises(ApprovalNotFound):
        decide(conn, 1, approval_id + 99, True, handlers, clock)
    with pytest.raises(ApprovalNotFound):
        decide(conn, 2, approval_id, True, handlers, clock)  # a request of business 1 is invisible to business 2
    assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"


def test_decide_checks_its_arguments(conn, clock, handlers):
    approval_id = ask(conn, clock)
    for bad in (1, 0, "true", None):
        with pytest.raises(ValueError):
            decide(conn, 1, approval_id, bad, handlers, clock)
    assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"


def test_note_is_cut_at_200_characters(conn, clock, handlers):
    approval_id = ask(conn, clock)
    decide(conn, 1, approval_id, True, handlers, clock, note="x" * 500)
    assert len(approvals_module.get(conn, 1, approval_id)["decision_note"]) <= 200


def test_a_handler_that_crashes_leaves_everything_unchanged(conn, clock):
    class Crashing:
        def approve(self, conn, approval, clock):
            authorize_spend(conn, 1, approval["agent_id"], approval["amount_idr"], clock)  # writes a ledger row first
            raise RuntimeError("boom after the ledger row")

        def reject(self, conn, approval, clock):
            raise RuntimeError("boom")

    registry = ApprovalHandlers()
    registry.register(KIND, Crashing())
    approval_id = ask(conn, clock)
    for approve in (True, False):
        with pytest.raises(RuntimeError):
            decide(conn, 1, approval_id, approve, registry, clock)
        assert not conn.in_transaction
        assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"
        assert ledger(conn) == [] and activity(conn) == []


def test_a_blocked_handler_leaves_the_request_pending_and_writes_nothing(conn, clock):
    class WritesThenBlocks:
        def approve(self, conn, approval, clock):
            authorize_spend(conn, 1, approval["agent_id"], 1_000, clock)  # a ledger row that must be rolled back
            raise ApprovalBlocked("belum bisa")

        reject = approve

    registry = ApprovalHandlers()
    registry.register(KIND, WritesThenBlocks())
    approval_id = ask(conn, clock)
    with pytest.raises(ApprovalBlocked, match="belum bisa"):
        decide(conn, 1, approval_id, True, registry, clock)
    assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"
    assert ledger(conn) == [] and activity(conn) == []


def test_a_request_that_can_never_be_applied_is_marked_failed_and_logged(conn, clock):
    class Gone:
        def approve(self, conn, approval, clock):
            authorize_spend(conn, 1, approval["agent_id"], 5_000, clock)  # rolled back with the handler
            raise ApprovalFailed("Pesanan sudah tidak ada")

        reject = approve

    registry = ApprovalHandlers()
    registry.register(KIND, Gone())
    approval_id = ask(conn, clock)
    result = decide(conn, 1, approval_id, True, registry, clock)
    assert (result.status, result.ok, result.cost_idr) == ("failed", False, 0)
    assert result.message == "Pesanan sudah tidak ada"
    assert approvals_module.get(conn, 1, approval_id)["status"] == "failed"
    assert ledger(conn) == []
    (entry,) = activity(conn)
    assert (entry["action"], entry["status"], entry["cost_idr"]) == ("Keputusan pemilik", "failed", 0)
    assert list_pending(conn, 1) == []


def test_an_unknown_kind_cannot_be_decided(conn, clock):
    approval_id = request_approval(conn, 1, 1, "mystery", "Sesuatu", {}, 0, clock)
    with pytest.raises(ApprovalBlocked):
        decide(conn, 1, approval_id, True, ApprovalHandlers(), clock)
    assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"


def test_handler_registry_rejects_bad_handlers():
    registry = ApprovalHandlers()
    with pytest.raises(ValueError):
        registry.register("", SpendHandler())
    with pytest.raises(ValueError):
        registry.register("x", object())
    registry.register("b", SpendHandler())
    registry.register("a", SpendHandler())
    assert registry.kinds() == ["a", "b"] and registry.get("zzz") is None


# ------------------------------------------------ the policy engine stays the gate --
def test_approval_over_the_daily_limit_is_refused_with_the_indonesian_message(conn, clock, handlers):
    approval_id = ask(conn, clock, amount=150_000)  # the limit is 100.000
    with pytest.raises(ApprovalBlocked) as caught:
        decide(conn, 1, approval_id, True, handlers, clock)
    assert str(caught.value) == "Melebihi batas harian Rp 100.000; naikkan batas lewat Aturan lalu setujui lagi"
    assert approvals_module.get(conn, 1, approval_id)["status"] == "pending"
    assert ledger(conn) == [] and activity(conn) == []  # a refused attempt writes nothing
    assert not conn.in_transaction


def test_the_limit_counts_what_was_already_spent_today(conn, clock, handlers):
    first = ask(conn, clock, amount=60_000, ref="a")
    second = ask(conn, clock, amount=60_000, ref="b")
    decide(conn, 1, first, True, handlers, clock)
    with pytest.raises(ApprovalBlocked):
        decide(conn, 1, second, True, handlers, clock)  # 60.000 + 60.000 > 100.000
    assert spent_today(conn, 1, clock) == 60_000
    assert remaining_today(conn, 1, clock) == 40_000


def test_after_the_owner_raises_the_limit_the_same_request_can_be_approved(conn, clock, handlers):
    approval_id = ask(conn, clock, amount=150_000)
    with pytest.raises(ApprovalBlocked):
        decide(conn, 1, approval_id, True, handlers, clock)
    set_policy(conn, 1, 200_000, [])  # the Rules dialog (Req 8.2): applies to the next action
    result = decide(conn, 1, approval_id, True, handlers, clock)
    assert result.status == "approved"
    assert [r["amount_idr"] for r in ledger(conn)] == [150_000]
    assert len(activity(conn)) == 1  # one row for the one decision, none for the refused attempt


def test_a_blocked_spend_kind_and_a_missing_policy_give_their_own_messages(conn, clock, handlers):
    approval_id = ask(conn, clock, amount=1_000)
    set_policy(conn, 1, 100_000, ["spend"])
    with pytest.raises(ApprovalBlocked, match="diblokir"):
        decide(conn, 1, approval_id, True, handlers, clock)
    conn.execute("DELETE FROM policy WHERE business_id = 1")
    with pytest.raises(ApprovalBlocked, match="belum diatur"):
        decide(conn, 1, approval_id, True, handlers, clock)
    assert ledger(conn) == []


def test_rejecting_never_needs_the_budget(conn, clock, handlers):
    approval_id = ask(conn, clock, amount=900_000)  # far above the limit
    result = decide(conn, 1, approval_id, False, handlers, clock)
    assert result.status == "rejected" and ledger(conn) == []


# ---------------------------------------------------------------- cool-down --
def test_a_rejection_starts_a_24_hour_cooldown_on_the_injected_clock(conn, clock, handlers):
    approval_id = ask(conn, clock, ref="product:1")
    assert not rejected_recently(conn, 1, KIND, "product:1", clock)
    decide(conn, 1, approval_id, False, handlers, clock)
    assert rejected_recently(conn, 1, KIND, "product:1", clock)
    assert not rejected_recently(conn, 1, KIND, "product:2", clock)  # another product
    assert not rejected_recently(conn, 2, KIND, "product:1", clock)  # another business
    assert not rejected_recently(conn, 1, "other", "product:1", clock)  # another kind
    clock.advance(24 * 3600 - 1)
    assert rejected_recently(conn, 1, KIND, "product:1", clock)
    clock.advance(2)
    assert not rejected_recently(conn, 1, KIND, "product:1", clock)


def test_an_approval_does_not_start_a_cooldown(conn, clock, handlers):
    approval_id = ask(conn, clock, ref="product:1", amount=1_000)
    decide(conn, 1, approval_id, True, handlers, clock)
    assert not rejected_recently(conn, 1, KIND, "product:1", clock)


# ------------------------------------------------------------------ events --
def test_a_decision_publishes_activity_and_approval_changed_only_after_it_is_saved(conn, clock, handlers):
    bus = RecordingBus()
    approval_id = ask(conn, clock, amount=150_000)
    with pytest.raises(ApprovalBlocked):
        decide(conn, 1, approval_id, True, handlers, clock, bus=bus)
    assert bus.events == []  # nothing was published for the refused attempt
    set_policy(conn, 1, 200_000, [])
    decide(conn, 1, approval_id, True, handlers, clock, bus=bus)
    assert [e["type"] for e in bus.events] == ["activity", "approval_changed"]
    changed = bus.events[1]
    assert (changed["approval_id"], changed["agent_id"], changed["status"], changed["pending_count"]) == (
        approval_id, 1, "approved", 0
    )


def test_waiting_agents_lists_agents_with_a_pending_request(conn, clock, handlers):
    assert approvals_module.waiting_agents(conn, 1) == {}
    approval_id = ask(conn, clock, subject="Pesan stok Teh")
    assert approvals_module.waiting_agents(conn, 1) == {1: "Menunggu OK: Pesan stok Teh"}
    decide(conn, 1, approval_id, False, handlers, clock)
    assert approvals_module.waiting_agents(conn, 1) == {}


def test_withdraw_pending_closes_a_moot_request_without_a_decision(conn, clock, handlers, handler):
    bus = RecordingBus()
    approval_id = ask(conn, clock, ref="product:1", bus=bus)
    assert approvals_module.withdraw_pending(conn, 1, KIND, "product:2", clock, "x") is None  # nothing pending there
    assert approvals_module.withdraw_pending(conn, 1, KIND, "product:1", clock, "Sudah selesai", bus=bus) == approval_id
    row = approvals_module.get(conn, 1, approval_id)
    assert (row["status"], row["decided_by"], row["decision_note"]) == ("failed", None, "Sudah selesai")
    assert row["decided_at"] is not None
    assert list_pending(conn, 1) == [] and ledger(conn) == [] and activity(conn) == []
    assert handler.approved == [] and handler.rejected == []  # no handler ran
    assert bus.events[-1]["status"] == "failed" and bus.events[-1]["pending_count"] == 0
    with pytest.raises(ApprovalNotPending):
        decide(conn, 1, approval_id, True, handlers, clock)
    assert not rejected_recently(conn, 1, KIND, "product:1", clock)  # not a rejection: no cool-down
    assert approvals_module.withdraw_pending(conn, 1, KIND, "product:1", clock, "again") is None
