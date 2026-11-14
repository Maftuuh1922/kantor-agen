import sqlite3
from datetime import datetime, timezone

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.events import EventBus
from skeleton.log import (
    daily_summary,
    install_append_only_triggers,
    recent,
    record,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Toko', 'toko')")
    c.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (1, 1, 'kasir', 'A', 0)")
    c.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (2, 1, 'stok', 'B', 1)")
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock(utc(2025, 3, 10, 5, 0, 0))


def _rec(conn, clock, action="sell", reason="why", cost=0, status="done", agent=1, business=1, bus=None):
    return record(conn, business, agent, action, reason, cost, status, clock, bus)


def test_record_returns_id_and_stores_row(conn, clock):
    rid = _rec(conn, clock, action="restock", reason="low stock", cost=5000)
    row = recent(conn, 1)[0]
    assert row["id"] == rid
    assert (row["agent_id"], row["action"], row["reason"], row["cost_idr"], row["status"]) == (
        1, "restock", "low stock", 5000, "done",
    )
    assert row["created_at"] == "2025-03-10T05:00:00.000000+00:00"


def test_recent_newest_first_limit_and_agent_filter(conn, clock):
    ids = []
    for i, agent in enumerate([1, 2, 1]):
        clock.advance(60)
        ids.append(_rec(conn, clock, action=f"a{i}", agent=agent))
    _rec(conn, clock, business=2, agent=None)  # other business is excluded
    assert [r["id"] for r in recent(conn, 1)] == ids[::-1]
    assert [r["id"] for r in recent(conn, 1, limit=2)] == ids[:0:-1]
    assert [r["id"] for r in recent(conn, 1, agent_id=1)] == [ids[2], ids[0]]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": ""},
        {"action": "   "},
        {"action": None},
        {"reason": ""},
        {"reason": 5},
        {"cost": -1},
        {"cost": 1.5},
        {"cost": True},
        {"cost": "10"},
        {"status": "bogus"},
        {"status": None},
    ],
)
def test_validation_errors(conn, clock, kwargs):
    with pytest.raises(ValueError):
        _rec(conn, clock, **kwargs)
    assert recent(conn, 1) == []


def test_record_publishes_event(conn, clock):
    bus = EventBus()
    sub = bus.subscribe()
    rid = _rec(conn, clock, action="sell", reason="r", cost=1000, status="done", bus=bus)
    event = sub._buffer.popleft()
    assert event == {
        "type": "activity",
        "id": rid,
        "business_id": 1,
        "agent_id": 1,
        "action": "sell",
        "reason": "r",
        "cost_idr": 1000,
        "status": "done",
        "created_at": "2025-03-10T05:00:00.000000+00:00",
    }


def test_invalid_record_publishes_nothing(conn, clock):
    bus = EventBus()
    sub = bus.subscribe()
    with pytest.raises(ValueError):
        _rec(conn, clock, status="nope", bus=bus)
    assert len(sub._buffer) == 0


def test_daily_summary_counts_and_pending(conn, clock):
    _rec(conn, clock, cost=1000, status="done")
    _rec(conn, clock, cost=2000, status="done")
    pid = _rec(conn, clock, action="order", reason="need", cost=9000, status="pending", agent=2)
    _rec(conn, clock, status="rejected", cost=500)
    _rec(conn, clock, business=2)  # other business ignored
    s = daily_summary(conn, 1, "2025-03-10")
    assert s["day"] == "2025-03-10"
    assert s["total_actions"] == 4
    assert s["by_status"] == {"done": 2, "pending": 1, "rejected": 1}
    assert s["pending"] == [{"id": pid, "agent_id": 2, "action": "order", "reason": "need"}]


def test_daily_summary_empty_day(conn):
    assert daily_summary(conn, 1, "2025-03-10") == {
        "day": "2025-03-10", "total_actions": 0, "by_status": {}, "total_spent_idr": 0, "pending": [],
    }


def test_spent_counts_only_done(conn, clock):
    _rec(conn, clock, cost=1000, status="done")
    for st in ("pending", "rejected", "failed", "info"):
        _rec(conn, clock, cost=7000, status=st)
    assert daily_summary(conn, 1, "2025-03-10")["total_spent_idr"] == 1000


def test_jakarta_day_boundary(conn, clock):
    # Jakarta is UTC+7: local midnight is 17:00 UTC.
    clock.set(utc(2025, 3, 10, 16, 59, 59))  # 23:59:59 on 10 Mar Jakarta
    _rec(conn, clock, action="late", cost=100)
    clock.set(utc(2025, 3, 10, 17, 0, 0))  # 00:00:00 on 11 Mar Jakarta
    _rec(conn, clock, action="midnight", cost=200)
    clock.set(utc(2025, 3, 10, 17, 30, 0))  # 00:30 on 11 Mar Jakarta
    _rec(conn, clock, action="early", cost=300)
    clock.set(utc(2025, 3, 11, 16, 59, 59))  # still 11 Mar Jakarta
    _rec(conn, clock, action="end", cost=400)

    d10 = daily_summary(conn, 1, "2025-03-10")
    d11 = daily_summary(conn, 1, "2025-03-11")
    d12 = daily_summary(conn, 1, "2025-03-12")
    assert (d10["total_actions"], d10["total_spent_idr"]) == (1, 100)
    assert (d11["total_actions"], d11["total_spent_idr"]) == (3, 900)
    assert d12["total_actions"] == 0


def test_daily_summary_rejects_bad_day(conn):
    with pytest.raises(ValueError):
        daily_summary(conn, 1, "10-03-2025")


def test_triggers_block_update_and_delete(conn, clock):
    install_append_only_triggers(conn)
    install_append_only_triggers(conn)  # idempotent
    rid = _rec(conn, clock)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE activity SET reason = 'x' WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM activity WHERE id = ?", (rid,))
    assert recent(conn, 1)[0]["reason"] == "why"
    _rec(conn, clock)  # inserts still work
    assert len(recent(conn, 1)) == 2


def test_module_exposes_no_update_or_delete():
    from skeleton.log import activity

    public = [n for n in dir(activity) if not n.startswith("_")]
    assert not [n for n in public if "update" in n.lower() or "delete" in n.lower() or "remove" in n.lower()]
