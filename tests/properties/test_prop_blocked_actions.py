"""Property test: blocked actions never execute (Req 5.3, 11.1).

Self-contained: own helpers, no shared conftest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hypothesis import given, settings, strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.policy.engine import ALLOWED_KINDS, authorize, set_policy

BUSINESS_ID = 1
AGENT_ID = 1
KINDS = sorted(ALLOWED_KINDS)
MAX_SPEND = 100_000
HUGE_LIMIT = 10**15


@dataclass
class Task:
    agent_id: int
    kind: str
    cost_idr: int
    payload: dict = field(default_factory=dict)


def _new_conn(daily_limit: int, blocked: list[str]):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (?, 'Warung', 'warung')", (BUSINESS_ID,))
    conn.execute(
        "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, ?, 'finance', 'Sari', 0)",
        (AGENT_ID, BUSINESS_ID),
    )
    set_policy(conn, BUSINESS_ID, daily_limit, blocked)
    return conn


def _counts(conn) -> tuple[int, int]:
    ledger = conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]
    outbox = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    return ledger, outbox


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
blocked_sets = st.lists(st.sampled_from(KINDS), unique=True, max_size=len(KINDS))

# Includes very large limits as well as small ones that make limit rejections likely.
daily_limits = st.one_of(
    st.integers(min_value=0, max_value=500_000),
    st.integers(min_value=0, max_value=HUGE_LIMIT),
    st.just(0),
    st.just(HUGE_LIMIT),
)

unknown_kinds = st.text(max_size=20).filter(lambda k: k not in ALLOWED_KINDS)

payloads = st.fixed_dictionaries(
    {},
    optional={
        "channel": st.text(max_size=10),
        "recipient": st.text(max_size=20),
        "body": st.text(max_size=40),
    },
)


@st.composite
def tasks(draw) -> Task:
    """A task whose cost is valid for its kind (unknown kinds get any cost)."""
    kind = draw(st.one_of(st.sampled_from(KINDS), unknown_kinds))
    if kind == "spend":
        cost = draw(st.integers(min_value=0, max_value=MAX_SPEND))
    elif kind in ALLOWED_KINDS:
        cost = 0
    else:
        cost = draw(st.integers(min_value=0, max_value=MAX_SPEND))
    return Task(agent_id=AGENT_ID, kind=kind, cost_idr=cost, payload=draw(payloads))


task_sequences = st.lists(tasks(), max_size=25)


# --------------------------------------------------------------------------- #
# Property 4
# --------------------------------------------------------------------------- #
# Property 4: Blocked actions never execute.
# For any action type in blocked_actions, authorize rejects the task and writes
# no spend_ledger row and no outbox row. Unknown kinds are rejected without
# writes, and non-blocked allowed kinds are never rejected because of the
# blocked list (only for limit or cost reasons).
# Validates: Requirements 5.3 (and 11.1: only allowed action types run).
@settings(max_examples=200, deadline=None)
@given(blocked=blocked_sets, limit=daily_limits, seq=task_sequences)
def test_blocked_actions_never_execute(blocked, limit, seq):
    conn = _new_conn(limit, blocked)
    try:
        clock = FakeClock()
        for task in seq:
            before = _counts(conn)
            decision = authorize(conn, BUSINESS_ID, task, clock)
            after = _counts(conn)

            if task.kind in blocked:
                # (a) blocked kinds are always rejected.
                assert not decision.allowed, task
                assert decision.outcome == "rejected"
                # (b) and they write nothing.
                assert after == before, (task, before, after)
            elif task.kind not in ALLOWED_KINDS:
                # (d) unknown kinds are rejected and write nothing.
                assert not decision.allowed, task
                assert after == before, (task, before, after)
            else:
                # (e) non-blocked allowed kinds with a valid cost are never
                # rejected because of the blocked list. Only a spend over the
                # daily limit may be rejected.
                assert decision.reason != "blocked action type", task
                if not decision.allowed:
                    assert task.kind == "spend", (task, decision)
                    assert decision.reason == "daily spending limit reached", (task, decision)
                    assert after == before, (task, before, after)
    finally:
        conn.close()


# Property 4 (c): the blocked check holds even with a huge limit and zero cost.
# A blocked kind must be rejected regardless of how much budget is available.
# Validates: Requirements 5.3
@settings(max_examples=200, deadline=None)
@given(
    blocked=blocked_sets.filter(bool),
    seed=st.integers(min_value=0, max_value=MAX_SPEND),
    payload=payloads,
    data=st.data(),
)
def test_blocked_kind_rejected_with_huge_limit_and_zero_cost(blocked, seed, payload, data):
    conn = _new_conn(HUGE_LIMIT, blocked)
    try:
        clock = FakeClock()
        kind = data.draw(st.sampled_from(blocked))
        before = _counts(conn)
        decision = authorize(conn, BUSINESS_ID, Task(AGENT_ID, kind, 0, payload), clock)
        assert not decision.allowed
        assert decision.outcome == "rejected"
        assert _counts(conn) == before

        # Same for a non-zero cost on a spend, even far below the limit.
        if kind == "spend":
            decision = authorize(conn, BUSINESS_ID, Task(AGENT_ID, kind, seed, payload), clock)
            assert not decision.allowed
            assert _counts(conn) == before
    finally:
        conn.close()


# Property 4 (outbox): a customer_message blocked by policy never reaches the outbox.
# Holds for any other blocked kinds, any payload, and any daily limit.
# Validates: Requirements 5.3, 11.1
@settings(max_examples=200, deadline=None)
@given(
    others=st.lists(st.sampled_from([k for k in KINDS if k != "customer_message"]), unique=True),
    limit=daily_limits,
    payload=payloads,
    repeats=st.integers(min_value=1, max_value=5),
)
def test_blocked_customer_message_never_reaches_outbox(others, limit, payload, repeats):
    conn = _new_conn(limit, ["customer_message", *others])
    try:
        clock = FakeClock()
        for _ in range(repeats):
            decision = authorize(
                conn, BUSINESS_ID, Task(AGENT_ID, "customer_message", 0, dict(payload)), clock
            )
            assert not decision.allowed
            assert decision.outcome != "queued_in_outbox"
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0
    finally:
        conn.close()
