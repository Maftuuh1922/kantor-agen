"""Property tests for the daily spending limit (Req 5.1, 11.1).

Self-contained: own helpers, no shared conftest.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.policy import authorize, set_policy

BUSINESS_ID = 1
AGENT_IDS = (1, 2, 3, 4)


@dataclass
class _Task:
    agent_id: int
    cost_idr: int
    kind: str = "spend"
    payload: dict = field(default_factory=dict)


def _make_conn(daily_limit):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id in AGENT_IDS:
        conn.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) "
            "VALUES (?, 1, 'finance', ?, ?)",
            (agent_id, f"Agent{agent_id}", agent_id),
        )
    set_policy(conn, BUSINESS_ID, daily_limit, [])
    return conn


def _ledger_sum(conn, day):
    return conn.execute(
        "SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger "
        "WHERE business_id = ? AND day = ?",
        (BUSINESS_ID, day),
    ).fetchone()[0]


def _ledger_count(conn):
    return conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]


def _run_day(conn, clock, limit, requests):
    """Send every request through authorize and assert the properties after each call."""
    day = clock.today()
    for agent_id, cost in requests:
        sum_before = _ledger_sum(conn, day)
        rows_before = _ledger_count(conn)
        remaining_before = max(0, limit - sum_before)

        decision = authorize(conn, BUSINESS_ID, _Task(agent_id, cost), clock)

        sum_after = _ledger_sum(conn, day)
        rows_after = _ledger_count(conn)

        # (a) the day's ledger total never exceeds the limit
        assert sum_after <= limit, (limit, sum_before, cost, sum_after)

        if decision.allowed:
            # (b) an allowed request fits in what was left
            assert cost <= remaining_before, (limit, sum_before, cost)
            assert rows_after == rows_before + 1
            assert sum_after == sum_before + cost
        else:
            # (c) a rejection happens only when the request does not fit
            assert sum_before + cost > limit, (limit, sum_before, cost, decision.reason)
            # (d) a rejection writes no ledger row
            assert rows_after == rows_before
            assert sum_after == sum_before


_limit = st.integers(min_value=0, max_value=2_000_000)
_request = st.tuples(
    st.sampled_from(AGENT_IDS),
    st.integers(min_value=0, max_value=500_000),
)
_requests = st.lists(_request, min_size=0, max_size=30)


@settings(max_examples=200, deadline=None)
@given(limit=_limit, requests=_requests)
def test_daily_spend_never_exceeds_limit_one_day(limit, requests):
    # Property 3: Daily spend never exceeds the limit.
    # For any sequence of spend requests within a day, the sum of allowed
    # amounts is at most daily_limit_idr, and no request that fits is rejected.
    # Validates: Requirements 5.1, 11.1
    conn = _make_conn(limit)
    try:
        clock = FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))  # 10:00 Jakarta
        _run_day(conn, clock, limit, requests)
    finally:
        conn.close()


@settings(max_examples=200, deadline=None)
@given(limit=_limit, day1=_requests, day2=_requests)
def test_daily_spend_limit_is_independent_per_day(limit, day1, day2):
    # Property 3 (two days): each Jakarta day is capped independently.
    # After the clock passes Jakarta midnight, the new day starts with the full
    # limit available, and the previous day's total is left unchanged.
    # Validates: Requirements 5.1, 11.1
    conn = _make_conn(limit)
    try:
        clock = FakeClock(datetime(2025, 1, 1, 16, 0, tzinfo=timezone.utc))  # 23:00 Jakarta
        first_day = clock.today()
        _run_day(conn, clock, limit, day1)
        day1_total = _ledger_sum(conn, first_day)

        clock.advance(3601)  # 17:00:01 UTC = 00:00:01 Jakarta, next day
        second_day = clock.today()
        assert second_day != first_day
        _run_day(conn, clock, limit, day2)

        # The second day's spending must not touch the first day's total.
        assert _ledger_sum(conn, first_day) == day1_total
        assert day1_total <= limit
        assert _ledger_sum(conn, second_day) <= limit
    finally:
        conn.close()
