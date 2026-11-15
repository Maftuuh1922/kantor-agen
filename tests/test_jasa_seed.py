"""Demo data of Kantor Jasa: what ``seed_jasa`` leaves behind and what it must never break (Req 10.1, 1.2).

The seed runs through the real onboarding (``bootstrap`` with ``JASA_TEMPLATE``) on an in-memory
database that has NO Jasa tables yet, so the test also proves that the template's seed creates them first.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from apps.kantor_jasa import data
from apps.kantor_jasa.roles import (
    CUSTOMER_NAMES,
    DEFAULT_AUTO_APPROVE_LIMIT_IDR,
    SERVICE_CATALOGUE,
    SimulatedPaymentFeed,
    build_roles,
)
from apps.kantor_jasa.seed import DEMO_PAYMENT_SEED, SEED_CUSTOMERS, seed_jasa
from apps.kantor_jasa.template import JASA_TEMPLATE
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.office.onboarding import bootstrap
from skeleton.runtime.models import BusinessContext

REPO = Path(__file__).resolve().parent.parent
START = datetime(2025, 3, 10, 7, 0, tzinfo=JAKARTA)  # a Monday, 07:00 Jakarta time
CATALOGUE = {s.name: s for s in SERVICE_CATALOGUE}


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def conn():
    c = db.connect(":memory:", check_same_thread=False)
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def seeded(conn, clock):
    """(connection, business id) after a real onboarding of a Jasa office."""
    return conn, bootstrap(conn, JASA_TEMPLATE, "Salon Mawar", clock)


def _end(booking: dict) -> datetime:
    return datetime.fromisoformat(booking["starts_at"]) + timedelta(minutes=booking["duration_min"])


def _jakarta_day(moment: str) -> date:
    return datetime.fromisoformat(moment).astimezone(JAKARTA).date()


# ---------------------------------------------------------------------- #
# What is there
# ---------------------------------------------------------------------- #
def test_the_template_seed_creates_the_jasa_tables_before_the_data(conn, clock):
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'customer'").fetchone() is None
    business_id = bootstrap(conn, JASA_TEMPLATE, "Salon Mawar", clock)
    assert len(data.list_customers(conn, business_id)) == 8


def test_eight_customers_known_to_the_simulated_feed(seeded):
    conn, bid = seeded
    customers = data.list_customers(conn, bid)
    assert [c["name"] for c in customers] == list(SEED_CUSTOMERS)
    assert set(SEED_CUSTOMERS) <= set(CUSTOMER_NAMES)
    assert all(c["phone"] for c in customers)


def test_services_and_prices_come_from_the_feed_catalogue(seeded):
    conn, bid = seeded
    bookings = data.list_bookings(conn, bid)
    assert {b["service"] for b in bookings} <= set(CATALOGUE)
    assert all(b["duration_min"] == CATALOGUE[b["service"]].duration_min for b in bookings)
    for invoice in data.list_invoices(conn, bid):
        booking = data.get_booking(conn, bid, invoice["booking_id"])
        assert invoice["description"] == booking["service"]
        assert invoice["amount_idr"] == CATALOGUE[booking["service"]].price_idr


def test_paid_invoices_exist_and_their_dates_are_in_the_past(seeded, clock):
    conn, bid = seeded
    paid = data.list_invoices(conn, bid, status="paid")
    assert len(paid) >= 5
    now = clock.now().isoformat(timespec="microseconds")
    for invoice in paid:
        assert invoice["created_at"] <= invoice["approved_at"] <= invoice["paid_at"] < now
        booking = data.get_booking(conn, bid, invoice["booking_id"])
        assert booking["status"] == "done"
    assert {i["approved_by"] for i in paid} == {"policy", "owner"}  # the big one was approved by hand


def test_one_done_booking_has_no_invoice_yet_so_billing_reacts(seeded):
    conn, bid = seeded
    billed = {i["booking_id"] for i in data.list_invoices(conn, bid)}
    unbilled = [b for b in data.list_bookings(conn, bid, status="done") if b["id"] not in billed]
    assert [b["service"] for b in unbilled] == ["Warna Rambut"]
    assert _jakarta_day(unbilled[0]["starts_at"]) == date(2025, 3, 9)  # yesterday: also gets a thank-you


def test_a_payment_reminder_is_due_and_stays_due_with_the_live_payment_feed(seeded, clock):
    conn, bid = seeded
    due = data.unpaid_overdue(conn, bid, clock, days=7)
    assert len(due) == 2 and all(i["status"] == "approved" for i in due)
    assert all(clock.now() - datetime.fromisoformat(i["approved_at"]) > timedelta(days=7) for i in due)
    feed = SimulatedPaymentFeed(seed=DEMO_PAYMENT_SEED)
    assert all(feed.delay_for(i["id"]) is None for i in due), "the simulator must not pay the invoices that are meant to be chased"
    ctx = BusinessContext(conn, bid, clock)
    assert feed.poll(ctx) == []


def test_exactly_one_draft_invoice_is_above_the_auto_approve_limit(seeded):
    conn, bid = seeded
    drafts = data.list_invoices(conn, bid, status="draft")
    assert len(drafts) == 1
    assert drafts[0]["amount_idr"] > DEFAULT_AUTO_APPROVE_LIMIT_IDR
    assert drafts[0]["approved_by"] is None and drafts[0]["approved_at"] is None


def test_tomorrow_has_two_confirmed_and_two_requested_bookings(seeded, clock):
    conn, bid = seeded
    tomorrow = date.fromisoformat(clock.today()) + timedelta(days=1)
    assert len(data.list_bookings(conn, bid, day=tomorrow, status="confirmed")) == 2
    requested = data.list_bookings(conn, bid, status="requested")
    assert len(requested) == 2 and all(_jakarta_day(b["starts_at"]) == tomorrow for b in requested)
    assert data.list_bookings(conn, bid, day=date.fromisoformat(clock.today())) == []  # nothing today


# ---------------------------------------------------------------------- #
# Invariants
# ---------------------------------------------------------------------- #
def test_no_two_confirmed_bookings_overlap_on_one_resource(seeded):
    conn, bid = seeded
    by_resource: dict[str, list[dict]] = {}
    for booking in data.list_bookings(conn, bid):
        if booking["status"] in ("confirmed", "done"):
            by_resource.setdefault(booking["resource"], []).append(booking)
    assert by_resource, "expected bookings"
    for resource, items in by_resource.items():
        items.sort(key=lambda b: b["starts_at"])
        for earlier, later in zip(items, items[1:]):
            assert _end(earlier) <= datetime.fromisoformat(later["starts_at"]), f"overlap on {resource}"


def test_one_clashing_request_exists_so_the_reschedule_path_runs(seeded):
    conn, bid = seeded
    confirmed = data.list_bookings(conn, bid, status="confirmed")
    clashing = []
    for request in data.list_bookings(conn, bid, status="requested"):
        start, end = datetime.fromisoformat(request["starts_at"]), _end(request)
        if any(
            c["resource"] == request["resource"]
            and datetime.fromisoformat(c["starts_at"]) < end
            and start < _end(c)
            for c in confirmed
        ):
            clashing.append(request)
    assert len(clashing) == 1
    with pytest.raises(data.BookingConflict):  # the data layer itself refuses to confirm it
        data.confirm_booking(conn, bid, clashing[0]["id"])


def test_invoice_immutability_is_still_enforced_after_seeding(seeded):
    conn, bid = seeded
    approved = data.list_invoices(conn, bid, status="approved")[0]
    paid = data.list_invoices(conn, bid, status="paid")[0]
    draft = data.list_invoices(conn, bid, status="draft")[0]
    for invoice in (approved, paid):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE invoice SET amount_idr = 1 WHERE id = ?", (invoice["id"],))
        with pytest.raises(sqlite3.DatabaseError, match="deleted"):
            conn.execute("DELETE FROM invoice WHERE id = ?", (invoice["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="edited"):  # no changing the amount while approving
        conn.execute("UPDATE invoice SET status = 'approved', amount_idr = 1 WHERE id = ?", (draft["id"],))
    with pytest.raises(data.InvoiceStateError):
        data.approve_invoice(conn, bid, paid["id"], "owner", FakeClock(START))
    assert data.get_invoice(conn, bid, approved["id"]) == approved  # nothing changed


def test_seed_is_deterministic_and_does_nothing_twice(conn, clock):
    first = bootstrap(conn, JASA_TEMPLATE, "Salon Satu", clock)
    snapshot = (
        data.list_customers(conn, first), data.list_bookings(conn, first), data.list_invoices(conn, first)
    )
    seed_jasa(conn, first, clock)  # a second call adds nothing
    assert snapshot == (
        data.list_customers(conn, first), data.list_bookings(conn, first), data.list_invoices(conn, first)
    )
    other = db.connect(":memory:")
    try:
        db.init_schema(other)
        second = bootstrap(other, JASA_TEMPLATE, "Salon Dua", clock)
        assert data.list_invoices(other, second) == data.list_invoices(conn, first)
        assert data.list_bookings(other, second) == data.list_bookings(conn, first)
    finally:
        other.close()


def test_the_seed_does_not_depend_on_the_time_of_day():
    """The plan is relative to the Jakarta date: it lands on the same days at 00:05 and at 23:55."""
    results = []
    for hour, minute in ((0, 5), (23, 55)):
        c = db.connect(":memory:")
        try:
            db.init_schema(c)
            bid = bootstrap(c, JASA_TEMPLATE, "Salon", FakeClock(datetime(2025, 3, 10, hour, minute, tzinfo=JAKARTA)))
            results.append([(b["service"], b["starts_at"], b["status"]) for b in data.list_bookings(c, bid)])
        finally:
            c.close()
    assert results[0] == results[1]


# ---------------------------------------------------------------------- #
# What the agents do with it on the first tick
# ---------------------------------------------------------------------- #
def test_the_first_triggers_react_to_the_seed_without_writing(seeded, clock):
    conn, bid = seeded
    roles = build_roles(conn, bid, clock)  # no feeds: only what the seed holds
    ctx = BusinessContext(conn, bid, clock)
    before = [dict(r) for r in conn.execute("SELECT * FROM invoice")]
    tasks = {role.name: role.triggers(ctx) for role in roles.values()}
    assert [t.payload["action"] for t in tasks["booking"]] == ["confirm", "confirm"]
    # One new invoice for the finished booking, and one request to the owner (task 14.2) for the large draft that
    # the seed left without a request. Neither writes anything yet.
    billing = {t.payload["action"]: t.payload for t in tasks["billing"]}
    assert sorted(billing) == ["approve", "invoice"]
    assert billing["invoice"]["amount_idr"] == 650_000
    assert [r["status"] for r in conn.execute("SELECT status FROM invoice WHERE id = ?", (billing["approve"]["invoice_id"],))] == ["draft"]
    kinds = sorted(t.payload["action"] for t in tasks["followup"])
    assert kinds == ["payment_reminder", "payment_reminder", "thank_you", "thank_you"]  # 2 overdue, 2 done yesterday
    assert before == [dict(r) for r in conn.execute("SELECT * FROM invoice")]


# ---------------------------------------------------------------------- #
# The seed goes through the data layer
# ---------------------------------------------------------------------- #
def test_seed_writes_only_through_the_data_layer():
    source = (REPO / "apps" / "kantor_jasa" / "seed.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", code, re.I), "seed.py must not write money tables with raw SQL"
    for call in ("data.add_customer", "data.request_booking", "data.confirm_booking",
                 "data.complete_booking", "data.create_invoice", "data.approve_invoice", "data.mark_paid"):
        assert call in code
    assert "init_jasa_schema" in code


def test_the_catalogue_is_reused_not_copied():
    source = (REPO / "apps" / "kantor_jasa" / "seed.py").read_text(encoding="utf-8")
    assert "SERVICE_CATALOGUE" in source
    assert not re.search(r"\b\d{2,3}_000\b", re.sub(r'""".*?"""', "", source, flags=re.S)), "no price literals in the seed"
