"""Property test: an owner approval never exceeds the daily spending limit (task 14.2; Req 5.1, 5.2, 7.1, 8.2).

Property: for any sequence of approval requests, daily-limit changes, owner decisions and passing time on an
in-memory database,

1. the day's ledger total never exceeds the limit that was in force at the moment of each approval;
2. an approval is refused exactly when the amount does not fit what is left today, and a refused or failed
   approval writes nothing (no ledger row, no activity row, the request stays ``pending``);
3. a rejection and a request that is not spent never write a ledger row;
4. each request is decided at most once: a second decision is a conflict that writes nothing, and the number of
   "Keputusan pemilik" activity rows equals the number of decisions that took effect;
5. a rejected request starts a cool-down of 24 hours of the injected clock for that thing (``rejected_recently``
   is true for exactly that long), and an agent that honors it files nothing while it lasts.

Self-contained: own helpers, no shared conftest. The "business" is generic (the skeleton knows no business type):
kind ``buy`` spends its amount through ``authorize_spend`` (the policy engine), kind ``note`` spends nothing.

**Validates: Requirements 5.1, 5.2, 7.1, 8.2**
"""
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.policy import (
    ApprovalBlocked,
    ApprovalHandlers,
    ApprovalNotPending,
    HandlerOutcome,
    authorize_spend,
    decide,
    rejected_recently,
    request_approval,
    set_policy,
)
from skeleton.policy import approvals as approvals_module

BUSINESS_ID = 1
AGENT_ID = 1
REFS = ("p1", "p2", "p3", "p4")
KINDS = ("buy", "note")
COOLDOWN = timedelta(hours=24)
START = datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc)  # 10:00 in Jakarta


class _Buy:
    """Approving spends the amount through the policy engine; rejecting costs nothing."""

    def approve(self, conn, approval, clock):
        authorize_spend(conn, approval["business_id"], approval["agent_id"], approval["amount_idr"], clock)
        return HandlerOutcome(message="dibeli", cost_idr=approval["amount_idr"])

    def reject(self, conn, approval, clock):
        return HandlerOutcome(message="dibatalkan")


class _Note:
    """Approving records a decision and spends nothing."""

    def approve(self, conn, approval, clock):
        return HandlerOutcome(message="dicatat")

    def reject(self, conn, approval, clock):
        return HandlerOutcome(message="dibatalkan")


def _make_conn(limit):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Usaha', 'generic')")
    conn.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (1, 1, 'buyer', 'Ani', 0)")
    set_policy(conn, BUSINESS_ID, limit, [])
    return conn


def _handlers():
    registry = ApprovalHandlers()
    registry.register("buy", _Buy())
    registry.register("note", _Note())
    return registry


def _ledger(conn):
    return [(r["day"], r["amount_idr"]) for r in conn.execute("SELECT day, amount_idr FROM spend_ledger ORDER BY id")]


def _snapshot(conn):
    return (
        _ledger(conn),
        [tuple(r) for r in conn.execute("SELECT id, status, decided_at, decision_note FROM approval ORDER BY id")],
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0],
    )


request_op = st.tuples(
    st.just("request"), st.sampled_from(KINDS), st.sampled_from(REFS), st.integers(min_value=0, max_value=300_000)
)
limit_op = st.tuples(st.just("limit"), st.integers(min_value=0, max_value=400_000))
decide_op = st.tuples(st.just("decide"), st.integers(min_value=0, max_value=100), st.booleans())
time_op = st.tuples(
    st.just("advance"), st.sampled_from([0, 60, 3600, 12 * 3600, 23 * 3600, 24 * 3600, 25 * 3600, 48 * 3600])
)
operations = st.lists(st.one_of(request_op, request_op, limit_op, decide_op, decide_op, decide_op, time_op), max_size=40)


@settings(max_examples=200, deadline=None)
@given(initial_limit=st.integers(min_value=0, max_value=400_000), ops=operations)
def test_approvals_never_exceed_the_limit_in_force(initial_limit, ops):
    conn = _make_conn(initial_limit)
    try:
        clock = FakeClock(START)
        handlers = _handlers()
        limit = initial_limit
        ids = []  # approval ids in creation order
        model = {}  # id -> {"kind", "ref", "amount", "status"}
        ledger = []  # the ledger the test expects: (day, amount)
        last_reject = {}  # (kind, ref) -> clock time of the owner's latest rejection
        decided_count = 0

        def spent_today_model():
            return sum(amount for day, amount in ledger if day == clock.today())

        for op in ops:
            name = op[0]
            if name == "limit":
                limit = op[1]
                set_policy(conn, BUSINESS_ID, limit, [])  # the Rules dialog: applies to the next action
            elif name == "advance":
                clock.advance(op[1])
            elif name == "request":
                _, kind, ref, amount = op
                cooling = (kind, ref) in last_reject and clock.now() - last_reject[(kind, ref)] < COOLDOWN
                assert rejected_recently(conn, BUSINESS_ID, kind, ref, clock) == cooling  # 5: exactly 24 hours
                before = _snapshot(conn)
                if cooling:
                    continue  # an agent that honors the cool-down files nothing
                approval_id = request_approval(
                    conn, BUSINESS_ID, AGENT_ID, kind, f"{kind} {ref}", {"summary": "s"}, amount, clock, ref=ref
                )
                open_same = [i for i in ids if model[i]["kind"] == kind and model[i]["ref"] == ref and model[i]["status"] == "pending"]
                if open_same:  # idempotent while pending: same id, nothing written
                    assert approval_id == open_same[0]
                    assert _snapshot(conn) == before
                else:
                    assert approval_id not in ids
                    ids.append(approval_id)
                    model[approval_id] = {"kind": kind, "ref": ref, "amount": amount, "status": "pending"}
            else:  # decide
                if not ids:
                    continue
                _, pick, approve = op
                approval_id = ids[pick % len(ids)]
                entry = model[approval_id]
                before = _snapshot(conn)
                if entry["status"] != "pending":
                    # 4: only a pending request can be decided, once
                    try:
                        decide(conn, BUSINESS_ID, approval_id, approve, handlers, clock)
                    except ApprovalNotPending as exc:
                        assert exc.status == entry["status"]
                    else:
                        raise AssertionError("a request was decided twice")
                    assert _snapshot(conn) == before
                    continue
                spends = approve and entry["kind"] == "buy"
                fits = spent_today_model() + entry["amount"] <= limit
                if spends and not fits:
                    # 2: refused exactly when it does not fit; nothing is written; still pending
                    try:
                        decide(conn, BUSINESS_ID, approval_id, True, handlers, clock)
                    except ApprovalBlocked as exc:
                        assert str(exc) == (
                            f"Melebihi batas harian Rp {limit:,}".replace(",", ".")
                            + "; naikkan batas lewat Aturan lalu setujui lagi"
                        )
                    else:
                        raise AssertionError("an approval went over the daily limit")
                    assert _snapshot(conn) == before
                    assert not conn.in_transaction
                    continue
                result = decide(conn, BUSINESS_ID, approval_id, approve, handlers, clock)
                decided_count += 1
                entry["status"] = "approved" if approve else "rejected"
                assert result.status == entry["status"]
                if spends:
                    ledger.append((clock.today(), entry["amount"]))
                    assert result.cost_idr == entry["amount"]
                    assert spent_today_model() <= limit  # 1: the day's total fits the limit in force now
                else:
                    assert result.cost_idr == 0
                if not approve:
                    last_reject[(entry["kind"], entry["ref"])] = clock.now()  # 5: the cool-down starts
                # 3: a rejection or a no-spend approval writes no ledger row; a spend writes exactly one
                assert _ledger(conn) == ledger

            # Invariants after every operation
            assert _ledger(conn) == ledger
            assert not conn.in_transaction
            stored = {r["id"]: r for r in conn.execute("SELECT id, status, decided_at, decided_by FROM approval")}
            assert set(stored) == set(ids)
            for approval_id, entry in model.items():
                row = stored[approval_id]
                assert row["status"] == entry["status"]
                assert (row["decided_at"] is None) == (entry["status"] == "pending")
                assert row["decided_by"] == (None if entry["status"] == "pending" else "owner")
            owner_rows = conn.execute(
                "SELECT COUNT(*) FROM activity WHERE action = ?", (approvals_module.DECISION_ACTION,)
            ).fetchone()[0]
            assert owner_rows == decided_count  # every decision that took effect is logged, once
            assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == decided_count  # and nothing else
    finally:
        conn.close()


@settings(max_examples=200, deadline=None)
@given(
    rejections=st.lists(
        st.tuples(st.sampled_from(REFS), st.integers(min_value=0, max_value=36 * 3600)), min_size=1, max_size=6
    ),
    probes=st.lists(
        st.sampled_from([0, 1, 3600, 12 * 3600, 24 * 3600 - 1, 24 * 3600, 24 * 3600 + 1, 30 * 3600, 72 * 3600]),
        min_size=1,
        max_size=6,
    ),
)
def test_a_rejection_blocks_new_requests_for_exactly_24_hours_of_the_injected_clock(rejections, probes):
    """5: after the owner rejects a thing, it is "recently rejected" for 24 hours, and only for that thing."""
    conn = _make_conn(100_000)
    try:
        clock = FakeClock(START)
        handlers = _handlers()
        rejected_at = {}  # ref -> time of its latest rejection
        for ref, wait in rejections:
            clock.advance(wait)
            approval_id = request_approval(conn, BUSINESS_ID, AGENT_ID, "buy", f"buy {ref}", {}, 1_000, clock, ref=ref)
            assert decide(conn, BUSINESS_ID, approval_id, False, handlers, clock).status == "rejected"
            rejected_at[ref] = clock.now()
        elapsed = 0
        for probe in sorted(probes):  # offsets from the last rejection, in time order
            clock.advance(probe - elapsed)
            elapsed = probe
            for ref in REFS:
                expected = ref in rejected_at and clock.now() - rejected_at[ref] < COOLDOWN
                assert rejected_recently(conn, BUSINESS_ID, "buy", ref, clock) == expected
                assert not rejected_recently(conn, BUSINESS_ID, "note", ref, clock)  # another kind
        assert conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0  # rejections never spend
    finally:
        conn.close()
