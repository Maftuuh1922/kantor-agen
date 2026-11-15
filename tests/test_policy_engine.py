"""Example-based tests for the policy engine (Req 5.1-5.5)."""

import json
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import datetime, timezone

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.policy import (
    ALLOWED_KINDS,
    Decision,
    authorize,
    remaining_today,
    set_policy,
    spent_today,
)


@dataclass
class FakeTask:
    kind: str
    cost_idr: object = 0
    agent_id: int = 1
    payload: dict = field(default_factory=dict)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Jasa', 'jasa')")
    c.execute(
        "INSERT INTO agent (id, business_id, role, name, desk_index) "
        "VALUES (1, 1, 'finance', 'Sari', 0)"
    )
    yield c
    c.close()


@pytest.fixture
def clock():
    # 2025-01-01 03:00 UTC = 10:00 in Jakarta
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


def _count(conn, table, business_id=None):
    if business_id is None:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE business_id = ?", (business_id,)
    ).fetchone()[0]


def _spend(conn, clock, cost, business_id=1):
    return authorize(conn, business_id, FakeTask("spend", cost), clock)


# --- Decision ---------------------------------------------------------------


def test_decision_helpers():
    allowed = Decision.allow()
    assert (allowed.allowed, allowed.outcome) == (True, "allowed")
    queued = Decision.allow(outcome="queued_in_outbox")
    assert queued.allowed and queued.outcome == "queued_in_outbox"
    rejected = Decision.reject("nope")
    assert (rejected.allowed, rejected.reason, rejected.outcome) == (False, "nope", "rejected")
    with pytest.raises(FrozenInstanceError):
        allowed.allowed = False  # type: ignore[misc]


def test_decision_rejects_inconsistent_values():
    with pytest.raises(ValueError):
        Decision.allow(outcome="rejected")
    with pytest.raises(ValueError):
        Decision.allow(outcome="bogus")


# --- Spend limit ------------------------------------------------------------


def test_spend_within_limit_is_allowed_and_ledgered(conn, clock):
    set_policy(conn, 1, 100_000, [])
    decision = _spend(conn, clock, 30_000)
    assert decision == Decision.allow()
    row = conn.execute("SELECT * FROM spend_ledger").fetchone()
    assert row["business_id"] == 1
    assert row["agent_id"] == 1
    assert row["amount_idr"] == 30_000
    assert row["day"] == "2025-01-01"
    assert row["created_at"] == clock.now().isoformat()
    assert spent_today(conn, 1, clock) == 30_000
    assert remaining_today(conn, 1, clock) == 70_000


def test_exact_limit_allowed_and_one_more_rejected(conn, clock):
    set_policy(conn, 1, 100_000, [])
    assert _spend(conn, clock, 60_000).allowed
    assert _spend(conn, clock, 40_000).allowed  # sum == limit
    assert spent_today(conn, 1, clock) == 100_000
    assert remaining_today(conn, 1, clock) == 0
    decision = _spend(conn, clock, 1)  # limit + 1
    assert not decision.allowed
    assert decision.reason == "daily spending limit reached"
    assert decision.outcome == "rejected"
    assert spent_today(conn, 1, clock) == 100_000
    assert _count(conn, "spend_ledger") == 2


def test_single_spend_over_limit_rejected_and_not_ledgered(conn, clock):
    set_policy(conn, 1, 100_000, [])
    decision = _spend(conn, clock, 100_001)
    assert decision.reason == "daily spending limit reached"
    assert _count(conn, "spend_ledger") == 0


def test_rejected_spend_does_not_use_up_budget(conn, clock):
    set_policy(conn, 1, 100, [])
    assert not _spend(conn, clock, 101).allowed
    assert _spend(conn, clock, 100).allowed


def test_limit_is_per_business(conn, clock):
    set_policy(conn, 1, 100, [])
    set_policy(conn, 2, 100, [])
    assert _spend(conn, clock, 100, business_id=1).allowed
    assert _spend(conn, clock, 100, business_id=2).allowed
    assert spent_today(conn, 1, clock) == 100
    assert spent_today(conn, 2, clock) == 100


def test_zero_limit_rejects_any_positive_spend(conn, clock):
    set_policy(conn, 1, 0, [])
    assert not _spend(conn, clock, 1).allowed
    assert _count(conn, "spend_ledger") == 0


def test_new_limit_applies_to_next_action(conn, clock):
    set_policy(conn, 1, 100, [])
    assert _spend(conn, clock, 100).allowed
    assert not _spend(conn, clock, 50).allowed
    set_policy(conn, 1, 200, [])
    assert _spend(conn, clock, 50).allowed
    assert remaining_today(conn, 1, clock) == 50


# --- Day rollover -----------------------------------------------------------


def test_day_rollover_at_jakarta_midnight_resets_spend(conn):
    # 2025-01-01 16:59:59 UTC = 23:59:59 Jakarta
    clock = FakeClock(datetime(2025, 1, 1, 16, 59, 59, tzinfo=timezone.utc))
    set_policy(conn, 1, 100_000, [])
    assert _spend(conn, clock, 100_000).allowed
    assert not _spend(conn, clock, 1).allowed
    assert clock.today() == "2025-01-01"

    clock.advance(1)  # 00:00:00 Jakarta on 2025-01-02
    assert clock.today() == "2025-01-02"
    assert spent_today(conn, 1, clock) == 0
    assert remaining_today(conn, 1, clock) == 100_000
    assert _spend(conn, clock, 100_000).allowed
    days = [r["day"] for r in conn.execute("SELECT day FROM spend_ledger ORDER BY id")]
    assert days == ["2025-01-01", "2025-01-02"]


def test_day_follows_jakarta_not_utc(conn):
    # 2025-01-01 20:00 UTC is already 2025-01-02 in Jakarta.
    clock = FakeClock(datetime(2025, 1, 1, 20, 0, tzinfo=timezone.utc))
    set_policy(conn, 1, 10, [])
    assert _spend(conn, clock, 10).allowed
    assert conn.execute("SELECT day FROM spend_ledger").fetchone()["day"] == "2025-01-02"


# --- Blocked and unknown kinds ----------------------------------------------


@pytest.mark.parametrize("kind", sorted(ALLOWED_KINDS))
def test_blocked_kind_is_rejected_and_writes_nothing(conn, clock, kind):
    set_policy(conn, 1, 100_000, [kind])
    task = FakeTask(kind, 500 if kind == "spend" else 0, payload={"body": "hi"})
    decision = authorize(conn, 1, task, clock)
    assert not decision.allowed
    assert decision.reason == "blocked action type"
    assert _count(conn, "spend_ledger") == 0
    assert _count(conn, "outbox") == 0


def test_blocking_one_kind_leaves_others_allowed(conn, clock):
    set_policy(conn, 1, 100_000, ["customer_message"])
    assert authorize(conn, 1, FakeTask("read"), clock).allowed
    assert authorize(conn, 1, FakeTask("draft"), clock).allowed
    assert _spend(conn, clock, 10).allowed


@pytest.mark.parametrize("kind", ["delete_db", "", "SPEND", "Spend", "send_email"])
def test_unknown_kind_is_rejected(conn, clock, kind):
    set_policy(conn, 1, 100_000, [])
    decision = authorize(conn, 1, FakeTask(kind, 100), clock)
    assert not decision.allowed
    assert decision.reason == "unknown action type"
    assert _count(conn, "spend_ledger") == 0
    assert _count(conn, "outbox") == 0


def test_corrupt_blocked_list_fails_closed(conn, clock):
    set_policy(conn, 1, 100_000, [])
    conn.execute("UPDATE policy SET blocked_actions = 'not json' WHERE business_id = 1")
    decision = authorize(conn, 1, FakeTask("read"), clock)
    assert not decision.allowed
    assert decision.reason == "policy invalid"


# --- Customer messages ------------------------------------------------------


def test_customer_message_goes_to_outbox_only(conn, clock):
    set_policy(conn, 1, 100_000, [])
    task = FakeTask(
        "customer_message",
        payload={"channel": "whatsapp", "recipient": "+62812", "body": "Stok sudah datang"},
    )
    decision = authorize(conn, 1, task, clock)
    assert decision.allowed
    assert decision.outcome == "queued_in_outbox"
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert (row["business_id"], row["agent_id"]) == (1, 1)
    assert row["channel"] == "whatsapp"
    assert row["recipient"] == "+62812"
    assert row["body"] == "Stok sudah datang"
    assert row["status"] == "pending"
    assert _count(conn, "spend_ledger") == 0


def test_customer_message_uses_safe_defaults(conn, clock):
    set_policy(conn, 1, 100_000, [])
    assert authorize(conn, 1, FakeTask("customer_message", payload={}), clock).allowed
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["channel"] == "unspecified"
    assert row["recipient"] == ""
    assert row["body"] == ""
    assert row["status"] == "pending"


def test_customer_message_with_non_string_payload_values(conn, clock):
    set_policy(conn, 1, 100_000, [])
    payload = {"channel": None, "recipient": 628123, "body": ["a"]}
    assert authorize(conn, 1, FakeTask("customer_message", payload=payload), clock).allowed
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["channel"] == "unspecified"
    assert row["recipient"] == "628123"
    assert row["body"] == "['a']"


def test_customer_message_is_queued_even_when_daily_limit_is_used_up(conn, clock):
    set_policy(conn, 1, 100, [])
    assert _spend(conn, clock, 100).allowed
    decision = authorize(conn, 1, FakeTask("customer_message", payload={"body": "x"}), clock)
    assert decision.outcome == "queued_in_outbox"
    assert spent_today(conn, 1, clock) == 100


# --- read / draft -----------------------------------------------------------


@pytest.mark.parametrize("kind", ["read", "draft"])
def test_read_and_draft_allowed_without_writes(conn, clock, kind):
    set_policy(conn, 1, 0, [])
    decision = authorize(conn, 1, FakeTask(kind), clock)
    assert decision == Decision.allow()
    assert _count(conn, "spend_ledger") == 0
    assert _count(conn, "outbox") == 0


# --- No policy row ----------------------------------------------------------


def test_no_policy_rejects_spend(conn, clock):
    decision = _spend(conn, clock, 10)
    assert not decision.allowed
    assert decision.reason == "policy not set"
    assert _count(conn, "spend_ledger") == 0


def test_no_policy_allows_cost_free_kinds(conn, clock):
    assert authorize(conn, 1, FakeTask("read"), clock).allowed
    assert authorize(conn, 1, FakeTask("draft"), clock).allowed
    decision = authorize(conn, 1, FakeTask("customer_message", payload={"body": "hi"}), clock)
    assert decision.outcome == "queued_in_outbox"
    assert _count(conn, "outbox") == 1
    assert _count(conn, "spend_ledger") == 0


def test_no_policy_zero_budget_helpers(conn, clock):
    assert spent_today(conn, 1, clock) == 0
    assert remaining_today(conn, 1, clock) == 0


# --- Invalid costs ----------------------------------------------------------


@pytest.mark.parametrize("cost", [-1, -50_000, 10.5, 100.0, True, False, "100", None, float("nan")])
def test_invalid_cost_rejected_for_spend(conn, clock, cost):
    set_policy(conn, 1, 1_000_000, [])
    decision = _spend(conn, clock, cost)
    assert not decision.allowed
    assert decision.reason == "invalid cost"
    assert _count(conn, "spend_ledger") == 0


@pytest.mark.parametrize("kind", ["read", "draft", "customer_message"])
@pytest.mark.parametrize("cost", [1, 500, -1, 2.5, True])
def test_non_spend_kinds_must_have_zero_cost(conn, clock, kind, cost):
    set_policy(conn, 1, 1_000_000, [])
    decision = authorize(conn, 1, FakeTask(kind, cost, payload={"body": "x"}), clock)
    assert not decision.allowed
    assert decision.reason == "invalid cost"
    assert _count(conn, "spend_ledger") == 0
    assert _count(conn, "outbox") == 0


def test_zero_cost_spend_is_allowed(conn, clock):
    set_policy(conn, 1, 100, [])
    assert _spend(conn, clock, 0).allowed
    assert spent_today(conn, 1, clock) == 0


# --- Transaction behavior ---------------------------------------------------


def test_authorize_leaves_no_open_transaction(conn, clock):
    set_policy(conn, 1, 100, [])
    _spend(conn, clock, 10)
    assert not conn.in_transaction
    _spend(conn, clock, 1_000)  # rejected
    assert not conn.in_transaction


def test_failed_ledger_insert_rolls_back(conn, clock):
    set_policy(conn, 1, 100, [])
    bad = FakeTask("spend", 10, agent_id=999)  # no such agent: FK violation
    with pytest.raises(Exception):
        authorize(conn, 1, bad, clock)
    assert not conn.in_transaction
    assert _count(conn, "spend_ledger") == 0


# --- set_policy -------------------------------------------------------------


def test_set_policy_creates_then_replaces(conn):
    set_policy(conn, 1, 50_000, ["spend", "read"])
    row = conn.execute("SELECT * FROM policy WHERE business_id = 1").fetchone()
    assert row["daily_limit_idr"] == 50_000
    assert json.loads(row["blocked_actions"]) == ["read", "spend"]

    set_policy(conn, 1, 75_000, [])
    assert _count(conn, "policy") == 1
    row = conn.execute("SELECT * FROM policy WHERE business_id = 1").fetchone()
    assert row["daily_limit_idr"] == 75_000
    assert json.loads(row["blocked_actions"]) == []


def test_set_policy_deduplicates_blocked_actions(conn):
    set_policy(conn, 1, 10, ["draft", "draft"])
    row = conn.execute("SELECT blocked_actions FROM policy WHERE business_id = 1").fetchone()
    assert json.loads(row["blocked_actions"]) == ["draft"]


@pytest.mark.parametrize("limit", [-1, 1.5, 100.0, True, "100", None])
def test_set_policy_rejects_bad_limit(conn, limit):
    with pytest.raises(ValueError):
        set_policy(conn, 1, limit, [])
    assert _count(conn, "policy") == 0


@pytest.mark.parametrize(
    "blocked",
    [["delete_db"], ["SPEND"], [1], [None], ["spend", "nope"], "spend"],
)
def test_set_policy_rejects_bad_blocked_actions(conn, blocked):
    with pytest.raises(ValueError):
        set_policy(conn, 1, 100, blocked)
    assert _count(conn, "policy") == 0


def test_set_policy_accepts_zero_limit(conn):
    set_policy(conn, 1, 0, [])
    assert conn.execute("SELECT daily_limit_idr FROM policy").fetchone()[0] == 0
