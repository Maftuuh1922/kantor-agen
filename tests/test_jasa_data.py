"""Example-based tests for the Jasa data layer (Req 5.1, 6.2)."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from apps.kantor_jasa import data
from apps.kantor_jasa.data import (
    BookingConflict,
    BookingStateError,
    InvalidInput,
    InvoiceStateError,
    JasaError,
    UnknownBookingError,
    UnknownBusinessError,
    UnknownCustomerError,
    UnknownInvoiceError,
)
from apps.kantor_jasa.schema import init_jasa_schema, install_invoice_triggers
from skeleton.core import db
from skeleton.core.clock import FakeClock


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    init_jasa_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Salon A', 'jasa')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Salon B', 'jasa')")
    yield c
    c.close()


@pytest.fixture
def clock():
    # 2025-01-01 03:00 UTC = 10:00 in Jakarta
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


@pytest.fixture
def budi(conn):
    return data.add_customer(conn, 1, "Budi", "0812")


@pytest.fixture
def siti(conn):
    return data.add_customer(conn, 2, "Siti")


def at(hour, minute=0, day=6):
    """ISO time on 2025-01-<day> in Jakarta (a Monday for day=6)."""
    return f"2025-01-{day:02d}T{hour:02d}:{minute:02d}:00+07:00"


def book(conn, clock, customer, start, minutes=60, resource="Kursi 1", business=1, service="Potong"):
    return data.request_booking(conn, business, customer, service, resource, start, minutes, clock)


def confirmed(conn, clock, customer, start, minutes=60, resource="Kursi 1", business=1):
    booking_id = book(conn, clock, customer, start, minutes, resource, business)
    data.confirm_booking(conn, business, booking_id)
    return booking_id


def status_of(conn, table, row_id):
    return conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def approved_invoice(conn, clock, customer, amount=150000):
    invoice_id = data.create_invoice(conn, 1, customer, "Potong rambut", amount, clock=clock)
    data.approve_invoice(conn, 1, invoice_id, "owner", clock)
    return invoice_id


# ------------------------------------------------------------------- schema --


def _master(conn, names=None):
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    rows = [tuple(r) for r in rows]
    if names is not None:
        rows = [r for r in rows if r[1] in names]
    return rows


def test_init_jasa_schema_is_idempotent_and_leaves_skeleton_alone():
    c = db.connect(":memory:")
    db.init_schema(c)
    skeleton_objects = {r[1] for r in _master(c)}
    skeleton_before = _master(c)

    init_jasa_schema(c)
    after_first = _master(c)
    init_jasa_schema(c)
    install_invoice_triggers(c)
    assert _master(c) == after_first
    assert _master(c, skeleton_objects) == skeleton_before

    names = {r[1] for r in after_first}
    assert {"customer", "booking", "invoice"} <= names
    triggers = {r[1] for r in after_first if r[0] == "trigger"}
    assert len(triggers) == 4
    c.close()


def test_init_jasa_schema_does_not_end_an_open_transaction():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("BEGIN")
    init_jasa_schema(c)
    assert c.in_transaction
    c.execute("ROLLBACK")
    assert not any(r[1] == "invoice" for r in _master(c))  # the DDL rolled back too
    c.close()


# ---------------------------------------------------------------- customers --


def test_add_get_list_customers(conn, clock, budi, siti):
    other = data.add_customer(conn, 1, "  Ani  ", clock=clock)
    customer = data.get_customer(conn, 1, budi)
    assert (customer["name"], customer["phone"], customer["business_id"]) == ("Budi", "0812", 1)
    ani = data.get_customer(conn, 1, other)
    assert (ani["name"], ani["phone"]) == ("Ani", None)
    assert ani["created_at"] == "2025-01-01T03:00:00.000000+00:00"
    assert [c["id"] for c in data.list_customers(conn, 1)] == [budi, other]
    assert [c["id"] for c in data.list_customers(conn, 2)] == [siti]


def test_get_customer_is_scoped_to_business(conn, budi):
    assert data.get_customer(conn, 2, budi) is None
    assert data.get_customer(conn, 1, 999) is None


@pytest.mark.parametrize(
    "kwargs",
    [{"name": ""}, {"name": "   "}, {"name": None}, {"name": 5}, {"phone": ""}, {"phone": 812}, {"business_id": True}],
)
def test_add_customer_rejects_bad_input(conn, kwargs):
    args = dict(business_id=1, name="X", phone=None)
    args.update(kwargs)
    with pytest.raises(JasaError):
        data.add_customer(conn, **args)
    assert conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 0


def test_add_customer_unknown_business(conn):
    with pytest.raises(UnknownBusinessError):
        data.add_customer(conn, 99, "X")


# ------------------------------------------------------- booking: request ----


def test_request_booking_happy_path(conn, clock, budi):
    booking_id = book(conn, clock, budi, at(10), 45, service="  Potong  ", resource=" Kursi 1 ")
    booking = data.get_booking(conn, 1, booking_id)
    assert booking["status"] == "requested"
    assert (booking["service"], booking["resource"], booking["duration_min"]) == ("Potong", "Kursi 1", 45)
    assert booking["customer_id"] == budi
    # Stored in UTC: 10:00 Jakarta = 03:00 UTC
    assert booking["starts_at"] == "2025-01-06T03:00:00.000000+00:00"
    assert booking["created_at"] == "2025-01-01T03:00:00.000000+00:00"


def test_request_booking_accepts_z_suffix_and_aware_datetime(conn, clock, budi):
    a = book(conn, clock, budi, "2025-01-06T03:00:00Z")
    b = book(conn, clock, budi, datetime(2025, 1, 6, 10, 0, tzinfo=timezone(timedelta(hours=7))))
    assert data.get_booking(conn, 1, a)["starts_at"] == data.get_booking(conn, 1, b)["starts_at"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "2025-01-06T10:00:00"},  # no offset
        {"start": "2025-01-06"},  # date only
        {"start": "besok jam 10"},
        {"start": ""},
        {"start": 1736132400},
        {"start": datetime(2025, 1, 6, 10, 0)},  # naive datetime
        {"minutes": 0},
        {"minutes": -30},
        {"minutes": 481},
        {"minutes": True},
        {"minutes": 60.0},
        {"minutes": "60"},
        {"service": ""},
        {"service": "  "},
        {"resource": ""},
        {"resource": None},
    ],
)
def test_request_booking_rejects_bad_input(conn, clock, budi, kwargs):
    args = dict(start=at(10), minutes=60, service="Potong", resource="Kursi 1")
    args.update(kwargs)
    with pytest.raises(JasaError):
        book(conn, clock, budi, args["start"], args["minutes"], args["resource"], service=args["service"])
    assert conn.execute("SELECT COUNT(*) FROM booking").fetchone()[0] == 0


def test_request_booking_allows_8_hours_but_not_more(conn, clock, budi):
    assert book(conn, clock, budi, at(9), 480)
    with pytest.raises(InvalidInput):
        book(conn, clock, budi, at(9), 481)


def test_request_booking_customer_must_belong_to_business(conn, clock, budi, siti):
    with pytest.raises(UnknownCustomerError):
        book(conn, clock, siti, at(10), business=1)
    with pytest.raises(UnknownCustomerError):
        book(conn, clock, 999, at(10))
    with pytest.raises(InvalidInput):
        book(conn, clock, True, at(10))
    assert conn.execute("SELECT COUNT(*) FROM booking").fetchone()[0] == 0


# ------------------------------------------------------ booking: confirm -----


def test_confirm_booking_happy_path(conn, clock, budi):
    booking_id = book(conn, clock, budi, at(10))
    data.confirm_booking(conn, 1, booking_id)
    assert status_of(conn, "booking", booking_id) == "confirmed"


def test_exact_conflict_leaves_status_requested(conn, clock, budi):
    first = confirmed(conn, clock, budi, at(10))
    second = book(conn, clock, budi, at(10))
    with pytest.raises(BookingConflict):
        data.confirm_booking(conn, 1, second)
    assert status_of(conn, "booking", second) == "requested"
    assert status_of(conn, "booking", first) == "confirmed"
    assert not conn.in_transaction


@pytest.mark.parametrize(
    "start, minutes",
    [
        (at(10, 30), 60),  # starts inside the existing one
        (at(9, 30), 60),  # ends inside the existing one
        (at(10, 15), 15),  # fully inside
        (at(9), 180),  # fully contains it
        (at(10, 59), 5),  # one minute of overlap at the end
        (at(9, 55), 10),  # five minutes of overlap at the start
    ],
)
def test_overlapping_confirm_is_rejected(conn, clock, budi, start, minutes):
    confirmed(conn, clock, budi, at(10), 60)
    other = book(conn, clock, budi, start, minutes)
    with pytest.raises(BookingConflict):
        data.confirm_booking(conn, 1, other)
    assert status_of(conn, "booking", other) == "requested"


def test_back_to_back_bookings_are_allowed(conn, clock, budi):
    confirmed(conn, clock, budi, at(10), 60)  # 10:00-11:00
    after = book(conn, clock, budi, at(11), 30)  # starts exactly when the first ends
    before = book(conn, clock, budi, at(9, 30), 30)  # ends exactly when the first starts
    data.confirm_booking(conn, 1, after)
    data.confirm_booking(conn, 1, before)
    assert status_of(conn, "booking", after) == "confirmed"
    assert status_of(conn, "booking", before) == "confirmed"


def test_long_booking_earlier_in_the_day_is_checked(conn, clock, budi):
    confirmed(conn, clock, budi, at(8), 480)  # 08:00-16:00, the longest allowed
    other = book(conn, clock, budi, at(15, 30), 30)
    with pytest.raises(BookingConflict):
        data.confirm_booking(conn, 1, other)
    free = book(conn, clock, budi, at(16), 30)
    data.confirm_booking(conn, 1, free)


def test_same_time_on_a_different_resource_is_allowed(conn, clock, budi):
    confirmed(conn, clock, budi, at(10), resource="Kursi 1")
    other = book(conn, clock, budi, at(10), resource="Kursi 2")
    data.confirm_booking(conn, 1, other)
    assert status_of(conn, "booking", other) == "confirmed"


def test_same_resource_name_in_another_business_does_not_conflict(conn, clock, budi, siti):
    confirmed(conn, clock, siti, at(10), business=2)
    mine = book(conn, clock, budi, at(10))
    data.confirm_booking(conn, 1, mine)
    assert status_of(conn, "booking", mine) == "confirmed"


def test_cancelled_booking_does_not_block(conn, clock, budi):
    first = confirmed(conn, clock, budi, at(10))
    data.cancel_booking(conn, 1, first)
    second = book(conn, clock, budi, at(10))
    data.confirm_booking(conn, 1, second)
    assert status_of(conn, "booking", second) == "confirmed"


def test_requested_booking_does_not_block(conn, clock, budi):
    book(conn, clock, budi, at(10))  # stays requested
    second = book(conn, clock, budi, at(10))
    data.confirm_booking(conn, 1, second)
    assert status_of(conn, "booking", second) == "confirmed"


def test_done_booking_does_not_block(conn, clock, budi):
    # Only 'confirmed' bookings block a slot (a done booking is history).
    first = confirmed(conn, clock, budi, at(10))
    data.complete_booking(conn, 1, first)
    second = book(conn, clock, budi, at(10, 30))
    data.confirm_booking(conn, 1, second)
    assert status_of(conn, "booking", second) == "confirmed"


def test_confirm_booking_state_and_scope_errors(conn, clock, budi):
    booking_id = confirmed(conn, clock, budi, at(10))
    with pytest.raises(BookingStateError):
        data.confirm_booking(conn, 1, booking_id)  # already confirmed
    data.cancel_booking(conn, 1, booking_id)
    with pytest.raises(BookingStateError):
        data.confirm_booking(conn, 1, booking_id)  # cancelled stays cancelled
    with pytest.raises(UnknownBookingError):
        data.confirm_booking(conn, 1, 999)
    with pytest.raises(UnknownBookingError):
        data.confirm_booking(conn, 2, booking_id)
    with pytest.raises(InvalidInput):
        data.confirm_booking(conn, 1, True)
    assert status_of(conn, "booking", booking_id) == "cancelled"


def test_cancel_and_complete_transitions(conn, clock, budi):
    requested = book(conn, clock, budi, at(10))
    data.cancel_booking(conn, 1, requested)
    assert status_of(conn, "booking", requested) == "cancelled"
    with pytest.raises(BookingStateError):
        data.cancel_booking(conn, 1, requested)
    with pytest.raises(BookingStateError):
        data.complete_booking(conn, 1, requested)

    unconfirmed = book(conn, clock, budi, at(12))
    with pytest.raises(BookingStateError):
        data.complete_booking(conn, 1, unconfirmed)  # must be confirmed first

    ok = confirmed(conn, clock, budi, at(14))
    data.complete_booking(conn, 1, ok)
    assert status_of(conn, "booking", ok) == "done"
    with pytest.raises(BookingStateError):
        data.cancel_booking(conn, 1, ok)
    with pytest.raises(BookingStateError):
        data.complete_booking(conn, 1, ok)
    with pytest.raises(UnknownBookingError):
        data.cancel_booking(conn, 2, ok)


def test_list_bookings_filters_and_order(conn, clock, budi, siti):
    late = book(conn, clock, budi, at(15))
    early = book(conn, clock, budi, at(9))
    next_day = book(conn, clock, budi, at(9, day=7))
    book(conn, clock, siti, at(9), business=2)
    data.confirm_booking(conn, 1, early)
    assert [b["id"] for b in data.list_bookings(conn, 1)] == [early, late, next_day]
    assert [b["id"] for b in data.list_bookings(conn, 1, day="2025-01-06")] == [early, late]
    assert [b["id"] for b in data.list_bookings(conn, 1, day=date(2025, 1, 7))] == [next_day]
    assert [b["id"] for b in data.list_bookings(conn, 1, status="confirmed")] == [early]
    assert [b["id"] for b in data.list_bookings(conn, 1, day="2025-01-06", status="requested")] == [late]
    assert data.list_bookings(conn, 1, day="2025-02-01") == []
    with pytest.raises(InvalidInput):
        data.list_bookings(conn, 1, status="nope")
    with pytest.raises(InvalidInput):
        data.list_bookings(conn, 1, day="06-01-2025")


def test_list_bookings_day_is_a_jakarta_day(conn, clock, budi):
    late = book(conn, clock, budi, at(23, 30))  # 16:30 UTC, still Jan 6 in Jakarta
    after_midnight = book(conn, clock, budi, at(0, 30, day=7))  # 17:30 UTC Jan 6, Jan 7 in Jakarta
    assert [b["id"] for b in data.list_bookings(conn, 1, day="2025-01-06")] == [late]
    assert [b["id"] for b in data.list_bookings(conn, 1, day="2025-01-07")] == [after_midnight]


# ---------------------------------------------------- booking: next slot -----


def slot(conn, minutes=60, after=None, **kwargs):
    return data.next_free_slot(conn, 1, "Kursi 1", minutes, after or at(8), **kwargs)


def test_next_free_slot_empty_day_starts_at_opening(conn):
    result = slot(conn)
    assert result == datetime.fromisoformat(at(9))
    assert result.utcoffset() == timedelta(hours=7)


def test_next_free_slot_rounds_up_to_the_half_hour_grid(conn):
    assert slot(conn, after=at(10, 10)) == datetime.fromisoformat(at(10, 30))
    assert slot(conn, after=at(10, 30)) == datetime.fromisoformat(at(10, 30))  # on the grid: allowed
    assert slot(conn, after="2025-01-06T10:30:00.000001+07:00") == datetime.fromisoformat(at(11))
    assert slot(conn, after=at(10, 1)) == datetime.fromisoformat(at(10, 30))


def test_next_free_slot_skips_confirmed_bookings(conn, clock, budi):
    confirmed(conn, clock, budi, at(9), 60)  # 09:00-10:00
    confirmed(conn, clock, budi, at(10, 30), 30)  # 10:30-11:00
    # 10:00-11:00 would hit the second booking; 11:00 is the first free hour.
    assert slot(conn, 60) == datetime.fromisoformat(at(11))
    # A 30 minute slot fits between them at 10:00.
    assert slot(conn, 30) == datetime.fromisoformat(at(10))


def test_next_free_slot_allows_back_to_back(conn, clock, budi):
    confirmed(conn, clock, budi, at(9), 60)
    assert slot(conn, 60) == datetime.fromisoformat(at(10))  # starts when the booking ends
    confirmed(conn, clock, budi, at(11), 60)
    assert slot(conn, 60) == datetime.fromisoformat(at(10))  # ends when the next starts


def test_next_free_slot_ignores_requested_cancelled_other_resource_other_business(
    conn, clock, budi, siti
):
    book(conn, clock, budi, at(9))  # requested
    cancelled = confirmed(conn, clock, budi, at(9))
    data.cancel_booking(conn, 1, cancelled)
    confirmed(conn, clock, budi, at(9), resource="Kursi 2")
    confirmed(conn, clock, siti, at(9), business=2)
    assert slot(conn) == datetime.fromisoformat(at(9))


def test_next_free_slot_respects_working_hours(conn, clock, budi):
    # Before opening: wait for 09:00.
    assert slot(conn, after=at(6)) == datetime.fromisoformat(at(9))
    # The last start that still ends by 17:00 for a 60 minute slot is 16:00.
    assert slot(conn, 60, after=at(16)) == datetime.fromisoformat(at(16))
    # 16:10 rounds to 16:30, which would end at 17:30: move to the next day.
    assert slot(conn, 60, after=at(16, 10)) == datetime.fromisoformat(at(9, day=7))
    # After closing time: next day.
    assert slot(conn, 30, after=at(17)) == datetime.fromisoformat(at(9, day=7))
    assert slot(conn, 30, after=at(16, 30)) == datetime.fromisoformat(at(16, 30))
    # A fully booked day moves on to the next one.
    confirmed(conn, clock, budi, at(9), 480)  # 09:00-17:00
    assert slot(conn, 30) == datetime.fromisoformat(at(9, day=7))


def test_next_free_slot_custom_hours(conn):
    assert slot(conn, 60, hours=(13, 15)) == datetime.fromisoformat(at(13))
    assert slot(conn, 60, after=at(14, 30), hours=(13, 15)) == datetime.fromisoformat(at(13, day=7))
    assert slot(conn, 30, hours=(0, 24), after=at(0)) == datetime.fromisoformat(at(0))


def test_next_free_slot_none_when_duration_never_fits(conn):
    assert slot(conn, 120, hours=(9, 10)) is None


def test_next_free_slot_gives_up_after_the_search_window(conn, clock, budi):
    for day_offset in range(data.MAX_SEARCH_DAYS + 1):
        start = datetime.fromisoformat(at(9)) + timedelta(days=day_offset)
        confirmed(conn, clock, budi, start.isoformat(), 480)
    assert slot(conn, 30) is None


def test_next_free_slot_uses_jakarta_time_for_after_in_utc(conn):
    # 01:00 UTC = 08:00 Jakarta, before opening -> 09:00 Jakarta (02:00 UTC).
    assert slot(conn, after="2025-01-06T01:00:00+00:00") == datetime.fromisoformat(at(9))
    # 17:30 UTC on Jan 6 = 00:30 Jakarta on Jan 7 -> 09:00 Jakarta on Jan 7.
    assert slot(conn, after="2025-01-06T17:30:00+00:00") == datetime.fromisoformat(at(9, day=7))
    # 09:30 UTC = 16:30 Jakarta: a 30 minute slot still fits today.
    assert slot(conn, 30, after="2025-01-06T09:30:00+00:00") == datetime.fromisoformat(at(16, 30))


def test_next_free_slot_result_can_be_booked(conn, clock, budi):
    confirmed(conn, clock, budi, at(9), 60)
    found = slot(conn, 60)
    booking_id = book(conn, clock, budi, found, 60)
    data.confirm_booking(conn, 1, booking_id)
    assert slot(conn, 60) == found + timedelta(hours=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes": 0},
        {"minutes": True},
        {"minutes": 481},
        {"after": "2025-01-06T08:00:00"},
        {"after": None},
        {"resource": ""},
        {"hours": (17, 9)},
        {"hours": (9, 9)},
        {"hours": (9, 25)},
        {"hours": (-1, 9)},
        {"hours": (9.5, 17)},
        {"hours": (True, 17)},
        {"hours": (9,)},
        {"hours": 9},
    ],
)
def test_next_free_slot_rejects_bad_input(conn, kwargs):
    args = dict(minutes=60, after=at(8), resource="Kursi 1", hours=(9, 17))
    args.update(kwargs)
    with pytest.raises(InvalidInput):
        data.next_free_slot(conn, 1, args["resource"], args["minutes"], args["after"], hours=args["hours"])


# ------------------------------------------------------------------ invoices --


def test_create_invoice_happy_path(conn, clock, budi):
    booking_id = book(conn, clock, budi, at(10))
    invoice_id = data.create_invoice(conn, 1, budi, "  Potong rambut ", 150000, booking_id, clock)
    invoice = data.get_invoice(conn, 1, invoice_id)
    assert invoice["status"] == "draft"
    assert (invoice["customer_id"], invoice["booking_id"]) == (budi, booking_id)
    assert (invoice["description"], invoice["amount_idr"]) == ("Potong rambut", 150000)
    assert invoice["approved_by"] is None and invoice["approved_at"] is None
    assert invoice["paid_at"] is None
    assert invoice["created_at"] == "2025-01-01T03:00:00.000000+00:00"


def test_create_invoice_without_booking_and_with_zero_amount(conn, budi):
    invoice_id = data.create_invoice(conn, 1, budi, "Gratis konsultasi", 0)
    invoice = data.get_invoice(conn, 1, invoice_id)
    assert invoice["booking_id"] is None
    assert invoice["amount_idr"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": -1},
        {"amount": True},
        {"amount": 10.5},
        {"amount": 100.0},
        {"amount": "100"},
        {"amount": None},
        {"description": ""},
        {"description": "   "},
        {"description": None},
        {"booking": True},
        {"booking": "1"},
    ],
)
def test_create_invoice_rejects_bad_input(conn, budi, kwargs):
    args = dict(description="Potong", amount=1000, booking=None)
    args.update(kwargs)
    with pytest.raises(JasaError):
        data.create_invoice(conn, 1, budi, args["description"], args["amount"], args["booking"])
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 0


def test_create_invoice_customer_and_booking_must_belong_to_business(conn, clock, budi, siti):
    other_booking = book(conn, clock, siti, at(10), business=2)
    with pytest.raises(UnknownCustomerError):
        data.create_invoice(conn, 1, siti, "X", 1000)
    with pytest.raises(UnknownCustomerError):
        data.create_invoice(conn, 1, 999, "X", 1000)
    with pytest.raises(UnknownBookingError):
        data.create_invoice(conn, 1, budi, "X", 1000, other_booking)
    with pytest.raises(UnknownBookingError):
        data.create_invoice(conn, 1, budi, "X", 1000, 999)
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 0


def test_get_invoice_is_scoped_to_business(conn, clock, budi):
    invoice_id = data.create_invoice(conn, 1, budi, "X", 1000)
    assert data.get_invoice(conn, 2, invoice_id) is None
    assert data.get_invoice(conn, 1, 999) is None


def test_approve_invoice(conn, clock, budi):
    invoice_id = data.create_invoice(conn, 1, budi, "X", 1000)
    clock.set(datetime(2025, 1, 2, 5, 0, tzinfo=timezone.utc))
    data.approve_invoice(conn, 1, invoice_id, "policy", clock)
    invoice = data.get_invoice(conn, 1, invoice_id)
    assert invoice["status"] == "approved"
    assert invoice["approved_by"] == "policy"
    assert invoice["approved_at"] == "2025-01-02T05:00:00.000000+00:00"
    assert invoice["paid_at"] is None


@pytest.mark.parametrize("who", ["", "admin", "Owner", None, 1, True])
def test_approve_invoice_rejects_unknown_approver(conn, clock, budi, who):
    invoice_id = data.create_invoice(conn, 1, budi, "X", 1000)
    with pytest.raises(InvalidInput):
        data.approve_invoice(conn, 1, invoice_id, who, clock)
    assert status_of(conn, "invoice", invoice_id) == "draft"


def test_approve_invoice_only_from_draft_and_in_own_business(conn, clock, budi):
    invoice_id = approved_invoice(conn, clock, budi)
    with pytest.raises(InvoiceStateError):
        data.approve_invoice(conn, 1, invoice_id, "policy", clock)  # no second approval
    assert data.get_invoice(conn, 1, invoice_id)["approved_by"] == "owner"
    draft = data.create_invoice(conn, 1, budi, "Y", 5)
    with pytest.raises(UnknownInvoiceError):
        data.approve_invoice(conn, 2, draft, "owner", clock)
    with pytest.raises(UnknownInvoiceError):
        data.approve_invoice(conn, 1, 999, "owner", clock)
    data.void_invoice(conn, 1, draft)
    with pytest.raises(InvoiceStateError):
        data.approve_invoice(conn, 1, draft, "owner", clock)


def test_mark_paid_flow(conn, clock, budi):
    invoice_id = approved_invoice(conn, clock, budi)
    clock.set(datetime(2025, 1, 3, 8, 0, tzinfo=timezone.utc))
    data.mark_paid(conn, 1, invoice_id, clock)
    invoice = data.get_invoice(conn, 1, invoice_id)
    assert invoice["status"] == "paid"
    assert invoice["paid_at"] == "2025-01-03T08:00:00.000000+00:00"
    with pytest.raises(InvoiceStateError):
        data.mark_paid(conn, 1, invoice_id, clock)  # already paid
    with pytest.raises(InvoiceStateError):
        data.void_invoice(conn, 1, invoice_id)  # paid cannot be voided


def test_mark_paid_requires_approval_and_own_business(conn, clock, budi):
    draft = data.create_invoice(conn, 1, budi, "X", 1000)
    with pytest.raises(InvoiceStateError):
        data.mark_paid(conn, 1, draft, clock)
    approved = approved_invoice(conn, clock, budi)
    with pytest.raises(UnknownInvoiceError):
        data.mark_paid(conn, 2, approved, clock)
    data.void_invoice(conn, 1, approved)
    with pytest.raises(InvoiceStateError):
        data.mark_paid(conn, 1, approved, clock)  # void cannot be paid
    assert status_of(conn, "invoice", draft) == "draft"


def test_void_invoice_from_draft_and_approved(conn, clock, budi):
    draft = data.create_invoice(conn, 1, budi, "X", 1000)
    data.void_invoice(conn, 1, draft)
    assert data.get_invoice(conn, 1, draft)["status"] == "void"
    assert data.get_invoice(conn, 1, draft)["approved_by"] is None
    approved = approved_invoice(conn, clock, budi)
    data.void_invoice(conn, 1, approved)
    kept = data.get_invoice(conn, 1, approved)
    assert (kept["status"], kept["approved_by"]) == ("void", "owner")  # approval record is kept
    with pytest.raises(InvoiceStateError):
        data.void_invoice(conn, 1, approved)  # already void
    with pytest.raises(UnknownInvoiceError):
        data.void_invoice(conn, 2, draft)


def test_list_invoices(conn, clock, budi, siti):
    first = data.create_invoice(conn, 1, budi, "A", 1)
    second = approved_invoice(conn, clock, budi)
    data.create_invoice(conn, 2, siti, "Other", 1)
    assert [i["id"] for i in data.list_invoices(conn, 1)] == [first, second]
    assert [i["id"] for i in data.list_invoices(conn, 1, status="approved")] == [second]
    assert data.list_invoices(conn, 1, status="paid") == []
    with pytest.raises(InvalidInput):
        data.list_invoices(conn, 1, status="open")


def test_unpaid_overdue(conn, clock, budi):
    clock.set(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))
    old = approved_invoice(conn, clock, budi)
    clock.set(datetime(2025, 1, 2, 3, 0, tzinfo=timezone.utc))
    newer = approved_invoice(conn, clock, budi)
    paid = approved_invoice(conn, clock, budi)
    data.mark_paid(conn, 1, paid, clock)
    data.create_invoice(conn, 1, budi, "draft", 1)  # a draft is never overdue
    voided = approved_invoice(conn, clock, budi)
    data.void_invoice(conn, 1, voided)

    clock.set(datetime(2025, 1, 8, 3, 0, tzinfo=timezone.utc))  # exactly 7 days after `old`
    assert data.unpaid_overdue(conn, 1, clock) == []
    clock.set(datetime(2025, 1, 8, 3, 0, 1, tzinfo=timezone.utc))
    assert [i["id"] for i in data.unpaid_overdue(conn, 1, clock)] == [old]
    clock.set(datetime(2025, 1, 9, 3, 0, 1, tzinfo=timezone.utc))
    assert [i["id"] for i in data.unpaid_overdue(conn, 1, clock)] == [old, newer]
    assert [i["id"] for i in data.unpaid_overdue(conn, 1, clock, days=30)] == []
    assert data.unpaid_overdue(conn, 2, clock) == []
    for bad in (-1, True, 1.5, "7"):
        with pytest.raises(InvalidInput):
            data.unpaid_overdue(conn, 1, clock, days=bad)


def test_revenue_for_day_uses_jakarta_paid_at(conn, clock, budi):
    def pay(amount, when):
        invoice_id = approved_invoice(conn, clock, budi, amount)
        clock.set(when)
        data.mark_paid(conn, 1, invoice_id, clock)
        return invoice_id

    # 16:59 UTC Jan 1 = 23:59 Jakarta Jan 1; 17:00 UTC = 00:00 Jakarta Jan 2.
    pay(1000, datetime(2025, 1, 1, 16, 59, tzinfo=timezone.utc))
    pay(20000, datetime(2025, 1, 1, 17, 0, tzinfo=timezone.utc))
    pay(300000, datetime(2024, 12, 31, 17, 0, tzinfo=timezone.utc))  # Jan 1 00:00 Jakarta
    approved_invoice(conn, clock, budi, 999)  # approved, not paid
    voided = approved_invoice(conn, clock, budi, 888)
    data.void_invoice(conn, 1, voided)
    assert data.revenue_for_day(conn, 1, "2025-01-01") == 301000
    assert data.revenue_for_day(conn, 1, date(2025, 1, 2)) == 20000
    assert data.revenue_for_day(conn, 1, "2025-01-03") == 0
    assert data.revenue_for_day(conn, 2, "2025-01-01") == 0
    with pytest.raises(InvalidInput):
        data.revenue_for_day(conn, 1, "yesterday")
    with pytest.raises(InvalidInput):
        data.revenue_for_day(conn, 1, datetime(2025, 1, 1))


# --------------------------------------- invoice immutability in the database --

FROZEN_UPDATES = [
    ("amount_idr", "1"),
    ("description", "'edited'"),
    ("customer_id", "{other_customer}"),
    ("booking_id", "NULL"),
    ("approved_by", "'policy'"),
    ("approved_at", "'2030-01-01T00:00:00.000000+00:00'"),
    ("created_at", "'2030-01-01T00:00:00.000000+00:00'"),
    ("business_id", "2"),
    ("id", "9999"),
]


@pytest.fixture
def frozen_invoices(conn, clock, budi):
    """One invoice each in status approved, paid and void, all with a booking."""
    other_customer = data.add_customer(conn, 1, "Ani")
    made = {"other_customer": other_customer}
    for status in ("approved", "paid", "void"):
        booking_id = book(conn, clock, budi, at(10))
        invoice_id = data.create_invoice(conn, 1, budi, "Potong", 150000, booking_id, clock)
        data.approve_invoice(conn, 1, invoice_id, "owner", clock)
        if status == "paid":
            data.mark_paid(conn, 1, invoice_id, clock)
        elif status == "void":
            data.void_invoice(conn, 1, invoice_id)
        made[status] = invoice_id
    return made


@pytest.mark.parametrize("status", ["approved", "paid", "void"])
@pytest.mark.parametrize("column, value", FROZEN_UPDATES)
def test_raw_update_of_frozen_column_aborts(conn, frozen_invoices, status, column, value):
    invoice_id = frozen_invoices[status]
    before = tuple(conn.execute("SELECT * FROM invoice WHERE id = ?", (invoice_id,)).fetchone())
    value = value.format(other_customer=frozen_invoices["other_customer"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE invoice SET {column} = {value} WHERE id = ?", (invoice_id,))
    assert tuple(conn.execute("SELECT * FROM invoice WHERE id = ?", (invoice_id,)).fetchone()) == before


@pytest.mark.parametrize("status", ["paid", "void"])
def test_raw_update_of_paid_at_aborts_once_paid_or_void(conn, frozen_invoices, status):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE invoice SET paid_at = '2030-01-01T00:00:00.000000+00:00' WHERE id = ?",
            (frozen_invoices[status],),
        )


def test_raw_multi_column_update_aborts_as_a_whole(conn, frozen_invoices):
    invoice_id = frozen_invoices["approved"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE invoice SET status = 'paid', paid_at = '2025-01-01T00:00:00.000000+00:00', "
            "amount_idr = 1 WHERE id = ?",
            (invoice_id,),
        )
    row = conn.execute("SELECT status, amount_idr, paid_at FROM invoice WHERE id = ?", (invoice_id,)).fetchone()
    assert tuple(row) == ("approved", 150000, None)


def test_bulk_update_with_one_frozen_row_aborts_all(conn, clock, budi, frozen_invoices):
    draft = data.create_invoice(conn, 1, budi, "Draft", 5)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE invoice SET amount_idr = amount_idr + 1")
    assert data.get_invoice(conn, 1, draft)["amount_idr"] == 5


@pytest.mark.parametrize("status", ["approved", "paid", "void"])
def test_raw_delete_of_non_draft_aborts(conn, frozen_invoices, status):
    with pytest.raises(sqlite3.IntegrityError, match="draft"):
        conn.execute("DELETE FROM invoice WHERE id = ?", (frozen_invoices[status],))
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM invoice")
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 3


def test_deleting_a_business_with_approved_invoices_is_blocked(conn, frozen_invoices):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM business WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 3


def test_a_no_op_update_of_an_approved_invoice_is_allowed(conn, frozen_invoices):
    invoice_id = frozen_invoices["approved"]
    conn.execute(
        "UPDATE invoice SET amount_idr = amount_idr, description = description, status = status WHERE id = ?",
        (invoice_id,),
    )


def test_draft_invoice_can_be_edited_and_deleted_with_raw_sql(conn, budi):
    invoice_id = data.create_invoice(conn, 1, budi, "X", 1000)
    conn.execute("UPDATE invoice SET amount_idr = 2000, description = 'Y' WHERE id = ?", (invoice_id,))
    assert data.get_invoice(conn, 1, invoice_id)["amount_idr"] == 2000
    conn.execute("DELETE FROM invoice WHERE id = ?", (invoice_id,))
    assert data.get_invoice(conn, 1, invoice_id) is None


def test_editing_a_draft_while_changing_status_aborts(conn, clock, budi):
    invoice_id = data.create_invoice(conn, 1, budi, "X", 1000)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE invoice SET status = 'approved', approved_by = 'owner', "
            "approved_at = '2025-01-01T00:00:00.000000+00:00', amount_idr = 1 WHERE id = ?",
            (invoice_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE invoice SET status = 'void', description = 'Y' WHERE id = ?", (invoice_id,))
    with pytest.raises(sqlite3.IntegrityError):  # a voided draft carries no approval
        conn.execute(
            "UPDATE invoice SET status = 'void', approved_by = 'owner', "
            "approved_at = '2025-01-01T00:00:00.000000+00:00' WHERE id = ?",
            (invoice_id,),
        )
    assert tuple(conn.execute("SELECT status, amount_idr FROM invoice").fetchone()) == ("draft", 1000)


ALL_STATUSES = ["draft", "approved", "paid", "void"]
ALLOWED = {("draft", "approved"), ("draft", "void"), ("approved", "paid"), ("approved", "void")}


def _invoice_in(conn, clock, customer, status):
    invoice_id = data.create_invoice(conn, 1, customer, "T", 1000, clock=clock)
    if status in ("approved", "paid", "void") and status != "void":
        data.approve_invoice(conn, 1, invoice_id, "owner", clock)
    if status == "paid":
        data.mark_paid(conn, 1, invoice_id, clock)
    if status == "void":
        data.void_invoice(conn, 1, invoice_id)
    return invoice_id


@pytest.mark.parametrize(
    "old, new",
    [(o, n) for o in ALL_STATUSES for n in ALL_STATUSES if o != n],
)
def test_raw_status_transitions(conn, clock, budi, old, new):
    invoice_id = _invoice_in(conn, clock, budi, old)
    # Supply the fields each target status needs, so only the transition rule is under test.
    sets = {
        "draft": "status = 'draft'",
        "approved": "status = 'approved', approved_by = COALESCE(approved_by, 'owner'), "
        "approved_at = COALESCE(approved_at, '2025-01-01T00:00:00.000000+00:00')",
        "paid": "status = 'paid', paid_at = '2025-01-02T00:00:00.000000+00:00'",
        "void": "status = 'void'",
    }[new]
    sql = f"UPDATE invoice SET {sets} WHERE id = ?"
    if (old, new) in ALLOWED:
        conn.execute(sql, (invoice_id,))
        assert status_of(conn, "invoice", invoice_id) == new
    else:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (invoice_id,))
        assert status_of(conn, "invoice", invoice_id) == old


# ----------------------------------------------- other database constraints --


def test_database_rejects_inconsistent_invoice_rows(conn, budi):
    cols = "(business_id, customer_id, description, amount_idr, status, approved_by, approved_at, paid_at, created_at)"
    ts = "'2025-01-01T00:00:00.000000+00:00'"

    def insert(values):
        conn.execute(f"INSERT INTO invoice {cols} VALUES ({values})")

    insert(f"1, {budi}, 'ok', 100, 'draft', NULL, NULL, NULL, {ts}")
    bad_rows = [
        f"1, {budi}, 'x', 10.5, 'draft', NULL, NULL, NULL, {ts}",  # fractional rupiah
        f"1, {budi}, 'x', -1, 'draft', NULL, NULL, NULL, {ts}",
        f"1, {budi}, 'x', 1, 'open', NULL, NULL, NULL, {ts}",  # unknown status
        f"1, {budi}, 'x', 1, 'approved', NULL, NULL, NULL, {ts}",  # approved without approver
        f"1, {budi}, 'x', 1, 'approved', 'admin', {ts}, NULL, {ts}",  # unknown approver
        f"1, {budi}, 'x', 1, 'draft', 'owner', {ts}, NULL, {ts}",  # draft with approval
        f"1, {budi}, 'x', 1, 'approved', 'owner', NULL, NULL, {ts}",  # approver without time
        f"1, {budi}, 'x', 1, 'paid', 'owner', {ts}, NULL, {ts}",  # paid without paid_at
        f"1, {budi}, 'x', 1, 'approved', 'owner', {ts}, {ts}, {ts}",  # paid_at while not paid
        f"1, {budi}, '  ', 1, 'draft', NULL, NULL, NULL, {ts}",  # blank description
        f"2, {budi}, 'x', 1, 'draft', NULL, NULL, NULL, {ts}",  # customer of another business
    ]
    for values in bad_rows:
        with pytest.raises(sqlite3.IntegrityError):
            insert(values)
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 1


def test_database_rejects_bad_booking_rows(conn, budi, siti):
    cols = "(business_id, customer_id, service, resource, starts_at, duration_min, status, created_at)"
    ts = "'2025-01-06T03:00:00.000000+00:00'"
    for values in (
        f"1, {budi}, 's', 'r', {ts}, 0, 'requested', {ts}",
        f"1, {budi}, 's', 'r', {ts}, 481, 'requested', {ts}",
        f"1, {budi}, 's', 'r', {ts}, 60, 'pending', {ts}",
        f"1, {budi}, ' ', 'r', {ts}, 60, 'requested', {ts}",
        f"1, {siti}, 's', 'r', {ts}, 60, 'requested', {ts}",  # customer of another business
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO booking {cols} VALUES ({values})")


def test_invoice_booking_must_be_in_the_same_business(conn, clock, budi, siti):
    other_booking = book(conn, clock, siti, at(10), business=2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO invoice (business_id, customer_id, booking_id, description, amount_idr, "
            "status, created_at) VALUES (1, ?, ?, 'x', 1, 'draft', '2025-01-01T00:00:00.000000+00:00')",
            (budi, other_booking),
        )


# --------------------------------------------------------------- atomicity ---


def test_confirm_booking_is_atomic_when_the_update_fails(conn, clock, budi):
    booking_id = book(conn, clock, budi, at(10))
    conn.execute(
        "CREATE TRIGGER block_booking BEFORE UPDATE ON booking "
        "BEGIN SELECT RAISE(ABORT, 'booking update blocked'); END"
    )
    with pytest.raises(Exception, match="booking update blocked"):
        data.confirm_booking(conn, 1, booking_id)
    assert status_of(conn, "booking", booking_id) == "requested"
    assert not conn.in_transaction


def test_create_invoice_is_atomic_when_the_insert_fails(conn, budi):
    conn.execute(
        "CREATE TRIGGER block_invoice BEFORE INSERT ON invoice "
        "BEGIN SELECT RAISE(ABORT, 'invoice insert blocked'); END"
    )
    with pytest.raises(Exception, match="invoice insert blocked"):
        data.create_invoice(conn, 1, budi, "X", 1000)
    assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 0
    assert not conn.in_transaction


def test_failed_transition_leaves_no_open_transaction(conn, clock, budi):
    invoice_id = approved_invoice(conn, clock, budi)
    with pytest.raises(InvoiceStateError):
        data.approve_invoice(conn, 1, invoice_id, "owner", clock)
    assert not conn.in_transaction
    # The connection is still usable.
    data.mark_paid(conn, 1, invoice_id, clock)
    assert status_of(conn, "invoice", invoice_id) == "paid"


def test_jasa_error_hierarchy():
    for cls in (
        InvalidInput,
        UnknownBusinessError,
        UnknownCustomerError,
        UnknownBookingError,
        UnknownInvoiceError,
        BookingConflict,
        BookingStateError,
        InvoiceStateError,
    ):
        assert issubclass(cls, JasaError)

# --------------------------------------------------------------- reschedule --
def test_reschedule_moves_a_requested_booking_and_it_stays_requested(conn, clock, budi):
    booking_id = book(conn, clock, budi, at(10))
    data.reschedule_booking(conn, 1, booking_id, at(14))
    row = data.get_booking(conn, 1, booking_id)
    assert row["status"] == "requested"
    assert datetime.fromisoformat(row["starts_at"]) == datetime.fromisoformat(at(14))
def test_reschedule_refuses_other_statuses_and_unknown_or_foreign_bookings(conn, clock, budi, siti):
    done = confirmed(conn, clock, budi, at(10))
    with pytest.raises(BookingStateError):
        data.reschedule_booking(conn, 1, done, at(14))  # a confirmed booking never moves
    assert datetime.fromisoformat(data.get_booking(conn, 1, done)["starts_at"]) == datetime.fromisoformat(at(10))
    with pytest.raises(UnknownBookingError):
        data.reschedule_booking(conn, 1, 999, at(14))
    other = book(conn, clock, siti, at(10), business=2)
    with pytest.raises(UnknownBookingError):
        data.reschedule_booking(conn, 1, other, at(14))
    with pytest.raises(InvalidInput):
        data.reschedule_booking(conn, 1, other, "tomorrow")
