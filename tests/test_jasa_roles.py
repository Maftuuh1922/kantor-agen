"""Tests for the Kantor Jasa roles (Req 1.2, 3.2, 3.3, 5.4).

Real skeleton schema plus the Jasa schema in memory SQLite, real Manager and
Heartbeat, FakeClock and FakeLLMClient. No network and no real waiting.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.kantor_jasa import data
from apps.kantor_jasa.roles import (
    CUSTOMER_NAMES,
    JASA_ROLES,
    SERVICE_CATALOGUE,
    BillingRole,
    BookingRole,
    FollowUpRole,
    SimulatedBookingFeed,
    SimulatedPaymentFeed,
    build_roles,
    rupiah,
)
from apps.kantor_jasa.schema import init_jasa_schema
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.log.activity import recent
from skeleton.policy import set_policy
from skeleton.runtime import BusinessContext, FakeLLMClient, Heartbeat, Manager, RoleRegistry, TaskKind

BOOKING, BILLING, FOLLOWUP = 1, 2, 3
TABLES = ("business", "agent", "policy", "customer", "booking", "invoice", "activity", "outbox")
MAX_MESSAGE_CHARS = 60


def jakarta(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=JAKARTA)


def at(hour, minute=0, day=1):
    """ISO time on 2025-01-<day> in Jakarta (day 1 is a Wednesday)."""
    return f"2025-01-{day:02d}T{hour:02d}:{minute:02d}:00+07:00"


class ListFeed:
    """Booking feed that hands out whatever the test queued, once."""

    def __init__(self, *requests):
        self.queue = list(requests)

    def poll(self, ctx):
        requests, self.queue = self.queue, []
        return requests


class ListPayments(ListFeed):
    """Payment feed that hands out whatever the test queued, once."""


class ListBus:
    """Minimal event bus: remembers what was published."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def req(name="Budi Santoso", phone="0811", service="Potong Rambut", resource="Kursi 1",
        start=None, minutes=45):
    return {
        "customer_name": name, "phone": phone, "service": service, "resource": resource,
        "starts_at": start or at(13), "duration_min": minutes,
    }


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    init_jasa_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Salon Cantik', 'jasa')")
    for agent_id, role in ((BOOKING, "booking"), (BILLING, "billing"), (FOLLOWUP, "followup")):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, ?, ?, ?)",
            (agent_id, role, role.title(), agent_id - 1),
        )
    set_policy(c, 1, 100_000, [])
    yield c
    c.close()


@pytest.fixture
def clock():
    # 2025-01-01 03:00 UTC = 10:00 Jakarta (a Wednesday)
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


def make_env(conn, clock, *, booking_feed=None, payment_feed=None, llm=None, bus=None, **kwargs):
    llm = llm or FakeLLMClient()
    settings = Settings(model_low="low", model_strong="strong")
    roles = build_roles(conn, 1, clock, booking_feed=booking_feed, payment_feed=payment_feed, **kwargs)
    manager = Manager(conn, 1, clock, llm, settings, roles, bus)
    heartbeat = Heartbeat(conn, 1, clock, manager, roles, settings, bus)
    return SimpleNamespace(
        roles=roles, llm=llm, heartbeat=heartbeat, manager=manager, ctx=BusinessContext(conn, 1, clock)
    )


def customer(conn, clock, name="Budi Santoso", phone="0811"):
    return data.add_customer(conn, 1, name, phone, clock)


def finished(conn, clock, cust, service="Potong Rambut", start=None, minutes=45, resource="Kursi 1"):
    """A booking that is already 'done' (set up directly through the data layer)."""
    booking_id = data.request_booking(conn, 1, cust, service, resource, start or at(9), minutes, clock)
    data.confirm_booking(conn, 1, booking_id)
    data.complete_booking(conn, 1, booking_id)
    return booking_id


def approved(conn, clock, cust, amount=50_000, by="owner", description="Potong Rambut"):
    invoice_id = data.create_invoice(conn, 1, cust, description, amount, None, clock)
    data.approve_invoice(conn, 1, invoice_id, by, clock)
    return invoice_id


def pause_billing(conn):
    """Keep invoices out of the way of tests that look at messages only."""
    conn.execute("UPDATE agent SET paused = 1 WHERE id = ?", (BILLING,))


def table(conn, name):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY id")]


def bookings(conn):
    return table(conn, "booking")


def invoices(conn):
    return table(conn, "invoice")


def outbox(conn):
    return table(conn, "outbox")


def log(conn):
    return list(reversed(recent(conn, 1, limit=5000)))


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def start_of(booking):
    return datetime.fromisoformat(booking["starts_at"])


# ----------------------------------------------------------------- booking --


async def test_booking_registers_then_confirms(conn, clock):
    env = make_env(conn, clock, booking_feed=ListFeed(req()))

    results = await env.heartbeat.tick()

    assert [(r.status, r.message) for r in results] == [("done", "Permintaan Budi Santoso masuk, 01/01 13:00")]
    [cust] = table(conn, "customer")
    assert (cust["name"], cust["phone"]) == ("Budi Santoso", "0811")
    [booking] = bookings(conn)
    assert booking["status"] == "requested" and start_of(booking) == jakarta(2025, 1, 1, 13)
    assert (booking["service"], booking["resource"], booking["duration_min"]) == ("Potong Rambut", "Kursi 1", 45)

    results = await env.heartbeat.tick()
    assert [(r.status, r.message) for r in results] == [
        ("done", "Booking #1 Budi Santoso dikonfirmasi 01/01 13:00")
    ]
    assert bookings(conn)[0]["status"] == "confirmed"

    for _ in range(3):  # idempotent: nothing more to do
        clock.advance(300)
        assert await env.heartbeat.tick() == []
    assert len(bookings(conn)) == 1 and len(table(conn, "customer")) == 1

    rows = [r for r in log(conn) if r["agent_id"] == BOOKING]
    assert [r["reason"] for r in rows][0] == "Catat permintaan: Budi Santoso, Potong Rambut 01/01 13:00"
    assert {(r["action"], r["cost_idr"], r["status"]) for r in rows} == {
        ("Catat permintaan", 0, "done"), ("Konfirmasi booking", 0, "done"),  # friendly labels, task 14.1
    }
    assert len(env.llm.calls) == 0


async def test_booking_reuses_an_existing_customer_by_phone(conn, clock):
    old = customer(conn, clock, "Budi S.", "0811")
    env = make_env(conn, clock, booking_feed=ListFeed(req(name="Budi Santoso", phone="0811")))
    await env.heartbeat.tick()
    assert len(table(conn, "customer")) == 1 and bookings(conn)[0]["customer_id"] == old


async def test_booking_same_request_twice_is_registered_once(conn, clock):
    env = make_env(conn, clock, booking_feed=ListFeed(req(), req()))
    results = await env.heartbeat.tick()
    assert [r.status for r in results] == ["done", "info"]
    assert len(bookings(conn)) == 1


async def test_booking_conflict_proposes_next_free_slot_and_confirms_there(conn, clock):
    cust = customer(conn, clock, "Siti Aminah", "0822")
    held = data.request_booking(conn, 1, cust, "Creambath", "Kursi 1", at(13), 60, clock)
    data.confirm_booking(conn, 1, held)  # Kursi 1 is taken 13:00 to 14:00
    env = make_env(conn, clock, booking_feed=ListFeed(req(start=at(13, 30))))

    await env.heartbeat.tick()  # registers a request for 13:30 on Kursi 1
    results = await env.heartbeat.tick()  # 13:30 overlaps: propose 14:00

    assert [r.status for r in results] == ["done"]
    assert results[0].message == "Booking #2 bentrok, usul 01/01 14:00"
    assert results[0].data["proposed_start"] == jakarta(2025, 1, 1, 14).isoformat()
    second = bookings(conn)[1]
    assert second["status"] == "requested" and start_of(second) == jakarta(2025, 1, 1, 14)

    results = await env.heartbeat.tick()  # re-requested at the proposed slot, now confirmed
    assert [r.status for r in results] == ["done"] and "dikonfirmasi 01/01 14:00" in results[0].message
    assert [b["status"] for b in bookings(conn)] == ["confirmed", "confirmed"]
    assert len(bookings(conn)) == 2  # the same booking moved, no duplicates
    assert await env.heartbeat.tick() == []


async def test_booking_other_resource_does_not_conflict(conn, clock):
    cust = customer(conn, clock)
    held = data.request_booking(conn, 1, cust, "Creambath", "Kursi 1", at(13), 60, clock)
    data.confirm_booking(conn, 1, held)
    env = make_env(conn, clock, booking_feed=ListFeed(req(name="Siti", phone="0822", resource="Kursi 2", start=at(13))))
    await env.heartbeat.tick()
    await env.heartbeat.tick()
    assert bookings(conn)[1]["status"] == "confirmed" and start_of(bookings(conn)[1]) == jakarta(2025, 1, 1, 13)


async def test_booking_back_to_back_is_not_a_conflict(conn, clock):
    cust = customer(conn, clock)
    held = data.request_booking(conn, 1, cust, "Creambath", "Kursi 1", at(13), 60, clock)
    data.confirm_booking(conn, 1, held)
    env = make_env(conn, clock, booking_feed=ListFeed(req(name="Siti", phone="0822", start=at(14))))
    await env.heartbeat.tick()
    await env.heartbeat.tick()
    assert bookings(conn)[1]["status"] == "confirmed" and start_of(bookings(conn)[1]) == jakarta(2025, 1, 1, 14)


@pytest.mark.parametrize(
    "start, expected",
    [
        (at(16, 30), jakarta(2025, 1, 2, 9)),  # 16:30 + 45 min ends after closing: next morning
        (at(8, 0), jakarta(2025, 1, 1, 10, 0)),  # before opening and in the past (it is 10:00)
        (at(9, 0), jakarta(2025, 1, 1, 10, 0)),  # in the past: first free slot from now
    ],
)
async def test_booking_outside_hours_or_in_the_past_is_moved_to_a_free_slot(conn, clock, start, expected):
    env = make_env(conn, clock, booking_feed=ListFeed(req(start=start)))
    await env.heartbeat.tick()
    results = await env.heartbeat.tick()
    assert results[0].status == "done" and "usul" in results[0].message
    booking = bookings(conn)[0]
    assert booking["status"] == "requested" and start_of(booking) == expected
    await env.heartbeat.tick()
    assert bookings(conn)[0]["status"] == "confirmed"


async def test_booking_completes_after_the_end_time(conn, clock):
    cust = customer(conn, clock)
    booking_id = data.request_booking(conn, 1, cust, "Potong Rambut", "Kursi 1", at(11), 60, clock)
    data.confirm_booking(conn, 1, booking_id)
    pause_billing(conn)  # the invoice for the finished booking is tested elsewhere
    env = make_env(conn, clock)

    clock.set(jakarta(2025, 1, 1, 11, 59))
    assert await env.heartbeat.tick() == []
    assert bookings(conn)[0]["status"] == "confirmed"

    clock.set(jakarta(2025, 1, 1, 12, 0))  # exactly the end time
    results = await env.heartbeat.tick()
    assert [(r.status, r.message) for r in results] == [("done", "Booking #1 Budi Santoso selesai")]
    assert bookings(conn)[0]["status"] == "done"
    assert await env.heartbeat.tick() == []


async def test_booking_ignores_unusable_feed_requests(conn, clock):
    bad = [
        None, "text", {}, req(name=" "), req(minutes=0), req(minutes=True), req(minutes=9999),
        req(start="kemarin"), req(start="2025-01-01T13:00:00"),  # no UTC offset
    ]
    env = make_env(conn, clock, booking_feed=ListFeed(*bad))
    assert await env.heartbeat.tick() == []
    assert bookings(conn) == [] and table(conn, "customer") == []


async def test_booking_without_feed_registers_nothing(conn, clock):
    env = make_env(conn, clock)
    assert await env.heartbeat.tick() == []


# ---------------------------------------------------------------- billing --


async def test_billing_drafts_and_approves_by_policy_under_the_limit(conn, clock):
    cust = customer(conn, clock)
    booking_id = finished(conn, clock, cust, "Potong Rambut")
    env = make_env(conn, clock)

    results = await env.heartbeat.tick()

    assert [(r.status, r.message) for r in results] == [("done", "Invoice #1 Rp 50.000 disetujui")]
    [invoice] = invoices(conn)
    assert (invoice["status"], invoice["approved_by"], invoice["amount_idr"]) == ("approved", "policy", 50_000)
    assert (invoice["booking_id"], invoice["customer_id"], invoice["description"]) == (booking_id, cust, "Potong Rambut")
    assert invoice["approved_at"] is not None and invoice["paid_at"] is None
    for _ in range(3):
        clock.advance(600)
        assert await env.heartbeat.tick() == []
    assert len(invoices(conn)) == 1  # one invoice per booking
    assert len(env.llm.calls) == 0


async def test_billing_limit_boundary_is_inclusive(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Warna Rambut", at(9), 120)  # Rp 650.000
    finished(conn, clock, cust, "Creambath", at(11), 60, "Kursi 2")  # Rp 120.000
    env = make_env(conn, clock, auto_approve_limit_idr=120_000)
    results = await env.heartbeat.tick()
    by_amount = {i["amount_idr"]: i["status"] for i in invoices(conn)}
    assert by_amount == {650_000: "draft", 120_000: "approved"}  # exactly at the limit is approved
    assert sorted(r.status for r in results) == ["done", "pending"]


async def test_billing_default_limit_is_500k_and_large_invoice_stays_draft_and_pending(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Warna Rambut", at(9), 120)  # Rp 650.000 > Rp 500.000
    bus = ListBus()
    env = make_env(conn, clock, bus=bus)
    assert env.roles[BILLING].auto_approve_limit_idr == 500_000

    results = await env.heartbeat.tick()

    assert [(r.status, r.message) for r in results] == [("pending", "Invoice #1 Rp 650.000 menunggu pemilik")]
    assert len(results[0].message) <= MAX_MESSAGE_CHARS
    [invoice] = invoices(conn)
    assert (invoice["status"], invoice["approved_by"], invoice["approved_at"]) == ("draft", None, None)
    waiting = [e for e in bus.events if e["type"] == "agent_state" and e["agent_id"] == BILLING and e["state"] == "waiting"]
    assert len(waiting) == 1 and waiting[0]["bubble_text"] == "Invoice #1 menunggu OK pemilik"  # the amber marker: the short bubble

    for _ in range(5):  # it waits for the owner and does not repeat itself
        clock.advance(3600)
        assert await env.heartbeat.tick() == []
    assert invoices(conn)[0]["status"] == "draft" and len(invoices(conn)) == 1


async def test_billing_never_edits_an_approved_invoice(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Warna Rambut", at(9), 120)
    env = make_env(conn, clock)
    await env.heartbeat.tick()
    data.approve_invoice(conn, 1, 1, "owner", clock)  # the owner decides
    before = invoices(conn)

    for _ in range(4):
        clock.advance(7200)
        assert await env.heartbeat.tick() == []
    assert invoices(conn) == before and before[0]["approved_by"] == "owner"

    # the database itself refuses edits too
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE invoice SET amount_idr = 1 WHERE id = 1")
    assert invoices(conn) == before


async def test_billing_raised_limit_approves_an_existing_draft(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Creambath", at(9), 60)  # Rp 120.000
    env = make_env(conn, clock, auto_approve_limit_idr=100_000)
    results = await env.heartbeat.tick()
    assert [r.status for r in results] == ["pending"] and invoices(conn)[0]["status"] == "draft"

    env.roles[BILLING].auto_approve_limit_idr = 120_000
    results = await env.heartbeat.tick()
    assert [(r.status, r.message) for r in results] == [("done", "Invoice #1 Rp 120.000 disetujui")]
    assert (invoices(conn)[0]["status"], invoices(conn)[0]["approved_by"]) == ("approved", "policy")


async def test_billing_skips_services_without_price_and_voided_invoices(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Layanan Aneh", at(9))
    done = finished(conn, clock, cust, "Potong Rambut", at(10), 45, "Kursi 2")
    invoice_id = data.create_invoice(conn, 1, cust, "Potong Rambut", 50_000, done, clock)
    data.void_invoice(conn, 1, invoice_id)  # the owner voided it: do not invoice again
    env = make_env(conn, clock)
    assert await env.heartbeat.tick() == []
    assert [i["status"] for i in invoices(conn)] == ["void"]


async def test_billing_marks_reported_payments_paid(conn, clock):
    cust = customer(conn, clock)
    paid_me = approved(conn, clock, cust, 50_000, "policy")
    still_draft = data.create_invoice(conn, 1, cust, "Potong Rambut", 60_000, None, clock)
    feed = ListPayments({"invoice_id": paid_me}, {"invoice_id": still_draft}, {"invoice_id": 999}, {"x": 1}, None)
    env = make_env(conn, clock, payment_feed=feed)

    clock.advance(3600)
    results = await env.heartbeat.tick()

    assert [(r.status, r.message) for r in results] == [("done", "Invoice #1 lunas Rp 50.000")]
    first, second = invoices(conn)
    assert (first["status"], first["paid_at"] is not None) == ("paid", True)
    assert second["status"] == "draft"  # a draft cannot be paid
    assert data.revenue_for_day(conn, 1, "2025-01-01") == 50_000

    env.roles[BILLING].payment_feed = ListPayments({"invoice_id": paid_me})  # reported again
    assert await env.heartbeat.tick() == []
    assert invoices(conn)[0]["status"] == "paid"


def test_simulated_payment_feed_is_deterministic_and_has_slow_and_never_payers():
    a, b, other = SimulatedPaymentFeed(5), SimulatedPaymentFeed(5), SimulatedPaymentFeed(6)
    delays = [a.delay_for(i) for i in range(1, 200)]
    assert delays == [b.delay_for(i) for i in range(1, 200)]
    assert delays != [other.delay_for(i) for i in range(1, 200)]
    assert None in delays and any(d is not None and d < timedelta(days=2) for d in delays)
    assert any(d is not None and d >= timedelta(days=4) for d in delays)


async def test_simulated_payments_mark_invoices_paid_after_their_delay(conn, clock):
    cust = customer(conn, clock)
    feed = SimulatedPaymentFeed(11)
    ids = [approved(conn, clock, cust, 10_000 + i, "policy") for i in range(30)]
    approved_at = clock.now()
    env = make_env(conn, clock, payment_feed=feed)
    paid_at = {}
    for hours in range(0, 12 * 24 + 1):
        clock.set(approved_at + timedelta(hours=hours))
        for invoice in invoices(conn):
            expected = feed.delay_for(invoice["id"])
            due = expected is not None and clock.now() >= approved_at + expected
            if invoice["status"] == "paid":
                assert due  # never paid early
        results = await env.heartbeat.tick()
        assert all(r.status == "done" for r in results)
        for invoice in invoices(conn):
            if invoice["status"] == "paid":
                paid_at.setdefault(invoice["id"], hours)
    never = {i for i in ids if feed.delay_for(i) is None}
    assert never and set(paid_at) == set(ids) - never
    for invoice_id, hours in paid_at.items():  # paid at the first tick on or after approval + delay
        delay = feed.delay_for(invoice_id)
        assert timedelta(hours=hours) >= delay > timedelta(hours=hours - 1)


# --------------------------------------------------------------- follow-up --


def thanks_setup(conn, clock):
    """A booking done yesterday (Jan 1) for Budi; the clock is moved to Jan 2, 10:00."""
    cust = customer(conn, clock)
    booking_id = finished(conn, clock, cust, "Potong Rambut", at(9))
    pause_billing(conn)
    clock.set(jakarta(2025, 1, 2, 10, 0))
    return cust, booking_id


async def test_followup_thanks_the_day_after_and_only_once(conn, clock):
    cust, booking_id = thanks_setup(conn, clock)
    llm = FakeLLMClient(["Terima kasih Kak Budi, sampai jumpa lagi!"])
    env = make_env(conn, clock, llm=llm)

    clock.set(jakarta(2025, 1, 1, 23, 0))  # same day as the service: too early
    assert await env.heartbeat.tick() == []
    clock.set(jakarta(2025, 1, 2, 0, 0))  # the day after
    results = await env.heartbeat.tick()

    assert [(r.status, r.message) for r in results] == [("done", "queued in outbox")]
    [message] = outbox(conn)
    assert (message["channel"], message["status"], message["agent_id"]) == ("whatsapp", "pending", FOLLOWUP)
    assert message["recipient"] == "Budi Santoso (0811)"
    assert message["body"] == "Terima kasih Kak Budi, sampai jumpa lagi!"  # drafted by the model
    assert [c["model"] for c in llm.calls] == ["strong"]
    row = [r for r in log(conn) if r["action"] == "Kirim terima kasih"][0]
    assert row["reason"].startswith("Ucapan terima kasih Budi Santoso") and f"(booking #{booking_id})" in row["reason"]

    for hours in (1, 5, 24, 48, 72):  # later ticks, and a restarted set of roles, send nothing more
        clock.advance(hours * 3600)
        assert await env.heartbeat.tick() == []
    again = make_env(conn, clock, llm=llm)
    assert await again.heartbeat.tick() == []
    assert len(outbox(conn)) == 1 and len(llm.calls) == 1


async def test_followup_thanks_falls_back_to_a_template_when_the_model_fails(conn, clock):
    thanks_setup(conn, clock)
    llm = FakeLLMClient()
    llm.fail_next()
    env = make_env(conn, clock, llm=llm)

    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["done"]
    [message] = outbox(conn)
    assert message["status"] == "pending"
    assert "Budi Santoso" in message["body"] and "Potong Rambut" in message["body"]
    assert "Salon Cantik" in message["body"] and "lihat detail di kantor" not in message["body"]
    assert any(r["action"] == "Model AI" and r["status"] == "failed" for r in log(conn))


async def test_followup_does_not_thank_too_late_or_for_unfinished_services(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Potong Rambut", at(9, day=1))  # done on Jan 1
    data.confirm_booking(conn, 1, data.request_booking(conn, 1, cust, "Creambath", "Kursi 2", at(9, day=2), 60, clock))
    pause_billing(conn)
    clock.set(jakarta(2025, 1, 4, 10, 0))  # Jan 1 is 3 days back: too late to thank
    env = make_env(conn, clock)

    results = await env.heartbeat.tick()  # the booking role completes the Jan 2 booking
    assert [r.message for r in results] == ["Booking #2 Budi Santoso selesai"]
    assert outbox(conn) == []

    clock.set(jakarta(2025, 1, 4, 10, 30))  # the Jan 2 service is 2 days old: still within the thank-you window
    results = await env.heartbeat.tick()
    assert [r.message for r in results] == ["queued in outbox"] and len(outbox(conn)) == 1
    assert "Creambath" in log(conn)[-1]["reason"]


async def test_followup_reminder_after_overdue_uses_an_exact_template(conn, clock):
    cust = customer(conn, clock)
    invoice_id = approved(conn, clock, cust, 650_000, "owner", "Warna Rambut")
    approved_on = clock.now()
    llm = FakeLLMClient(["teks model yang tidak boleh dipakai"])
    env = make_env(conn, clock, llm=llm)

    clock.set(approved_on + timedelta(days=3))  # exactly 3 days: not yet overdue
    assert await env.heartbeat.tick() == []
    clock.set(approved_on + timedelta(days=3, minutes=1))
    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["done"]
    [message] = outbox(conn)
    assert (message["channel"], message["status"], message["recipient"]) == ("whatsapp", "pending", "Budi Santoso (0811)")
    assert message["body"] == (
        "Halo Kak Budi Santoso, kami mengingatkan tagihan Warna Rambut sebesar Rp 650.000 "
        f"(invoice #{invoice_id}) yang belum dibayar. Mohon konfirmasi jika sudah transfer. "
        "Terima kasih. - Salon Cantik"
    )
    row = [r for r in log(conn) if r["action"] == "Kirim pengingat"][0]
    assert row["reason"] == f"Pengingat bayar Budi Santoso Rp 650.000 (invoice #{invoice_id}) (masuk outbox, belum dikirim)"


async def test_followup_reminder_body_is_kept_when_the_model_is_down(conn, clock):
    cust = customer(conn, clock)
    approved(conn, clock, cust, 120_000, "owner", "Creambath")
    clock.advance(4 * 86400)
    llm = FakeLLMClient()
    llm.fail_next(3)
    env = make_env(conn, clock, llm=llm)
    await env.heartbeat.tick()
    assert "Rp 120.000" in outbox(conn)[0]["body"] and "lihat detail" not in outbox(conn)[0]["body"]


async def test_followup_reminds_each_invoice_at_most_once_per_three_days(conn, clock):
    cust = customer(conn, clock)
    first = approved(conn, clock, cust)
    start = clock.now() + timedelta(days=3, minutes=1)
    env = make_env(conn, clock)

    clock.set(start)
    await env.heartbeat.tick()
    assert len(outbox(conn)) == 1
    for minutes in (5, 60, 24 * 60, 2 * 24 * 60, 3 * 24 * 60 - 1):  # up to one minute short of 3 days
        clock.set(start + timedelta(minutes=minutes))
        assert await env.heartbeat.tick() == []
    assert len(outbox(conn)) == 1

    clock.set(start + timedelta(days=3))  # three days since the last reminder
    results = await env.heartbeat.tick()
    assert [r.status for r in results] == ["done"] and len(outbox(conn)) == 2
    assert all(f"(invoice #{first})" in m["body"] for m in outbox(conn))

    data.mark_paid(conn, 1, first, clock)  # paid: no more reminders
    clock.advance(10 * 86400)
    assert await env.heartbeat.tick() == []
    assert len(outbox(conn)) == 2


async def test_followup_daily_cap_defaults_to_five_and_carries_over(conn, clock):
    cust = customer(conn, clock)
    ids = [approved(conn, clock, cust, 10_000 + i) for i in range(7)]
    clock.advance(4 * 86400)
    env = make_env(conn, clock)
    assert env.roles[FOLLOWUP].max_messages_per_day == 5

    results = await env.heartbeat.tick()
    assert len(results) == 5 and len(outbox(conn)) == 5
    for _ in range(3):
        clock.advance(1800)
        assert await env.heartbeat.tick() == []  # cap reached for today
    assert len(outbox(conn)) == 5

    clock.advance(86400)  # next day: the two that were held back go out, the others wait for their 3 days
    results = await env.heartbeat.tick()
    assert len(results) == 2 and len(outbox(conn)) == 7
    sent = [int(m["body"].split("invoice #")[1].split(")")[0]) for m in outbox(conn)]
    assert sorted(sent) == ids


async def test_followup_cap_is_configurable_and_shared_by_thanks_and_reminders(conn, clock):
    cust = customer(conn, clock)
    approved(conn, clock, cust)  # approved Jan 1: overdue by Jan 6
    finished(conn, clock, cust, "Creambath", at(9, day=5), 60, "Kursi 2")  # done on Jan 5
    pause_billing(conn)
    clock.set(jakarta(2025, 1, 6, 10))
    env = make_env(conn, clock, max_messages_per_day=1)

    results = await env.heartbeat.tick()
    assert len(results) == 1 and len(outbox(conn)) == 1
    assert "mengingatkan tagihan" in outbox(conn)[0]["body"]  # reminders go first
    clock.advance(1800)
    assert await env.heartbeat.tick() == []

    clock.advance(86400)  # Jan 7: the reminder is not due again, the thank-you (2 days old) goes out
    results = await env.heartbeat.tick()
    assert len(results) == 1 and len(outbox(conn)) == 2
    assert "Creambath" in log(conn)[-1]["reason"]


async def test_followup_blocked_message_is_not_retried_all_day(conn, clock):
    cust = customer(conn, clock)
    approved(conn, clock, cust)
    clock.advance(4 * 86400)
    set_policy(conn, 1, 100_000, ["customer_message"])
    env = make_env(conn, clock)

    await env.heartbeat.tick()
    clock.advance(600)
    assert await env.heartbeat.tick() == []
    assert outbox(conn) == []
    assert [r["status"] for r in log(conn) if r["action"] in ("Kirim pengingat", "Kirim terima kasih")] == ["rejected"]

    set_policy(conn, 1, 100_000, [])  # allowed again, the next day
    clock.advance(86400)
    await env.heartbeat.tick()
    assert len(outbox(conn)) == 1


async def test_outbox_only_has_pending_rows_and_nothing_is_sent(conn, clock):
    cust = customer(conn, clock)
    approved(conn, clock, cust)
    finished(conn, clock, cust, "Potong Rambut", at(9), 45, "Kursi 2")
    clock.advance(4 * 86400)
    env = make_env(conn, clock)
    for _ in range(3):
        await env.heartbeat.tick()
        clock.advance(86400)
    box = outbox(conn)
    assert box and {m["status"] for m in box} == {"pending"}
    # there is no way to send from a role: no sending hook and no "sent" bookkeeping anywhere
    assert not [name for name in dir(FollowUpRole) if "send" in name.lower()]
    assert {r["status"] for r in log(conn)} <= {"done", "rejected", "pending", "info"}
    with pytest.raises(sqlite3.DatabaseError):  # the table itself only allows 'pending'
        conn.execute("UPDATE outbox SET status = 'sent' WHERE id = 1")


# ------------------------------------------------------------- purity (3.3) --


async def test_triggers_are_pure(conn, clock):
    cust = customer(conn, clock)
    held = data.request_booking(conn, 1, cust, "Potong Rambut", "Kursi 1", at(9), 45, clock)
    data.confirm_booking(conn, 1, held)  # confirmed, and over at 10:00 -> completion trigger
    data.request_booking(conn, 1, cust, "Creambath", "Kursi 2", at(13), 60, clock)  # confirm trigger
    finished(conn, clock, cust, "Cuci dan Blow", at(11, 0), 30, "Kursi 2")  # invoice trigger
    small = data.create_invoice(conn, 1, cust, "Potong Rambut", 50_000, None, clock)  # approve trigger
    old = approved(conn, clock, cust, 90_000, "owner", "Creambath")  # payment + reminder triggers
    clock.advance(5 * 86400)
    feed = SimulatedBookingFeed(3, chance=1.0)  # a register trigger on every working-hours poll
    pay = ListPayments({"invoice_id": old})
    env = make_env(conn, clock, booking_feed=feed, payment_feed=pay)
    before = counts(conn)
    snapshot = (bookings(conn), invoices(conn), table(conn, "customer"))

    emitted = {agent_id: role.triggers(env.ctx) for agent_id, role in env.roles.items()}

    assert all(emitted.values())  # every role had something to do
    assert counts(conn) == before
    assert (bookings(conn), invoices(conn), table(conn, "customer")) == snapshot
    assert len(env.llm.calls) == 0
    for agent_id, tasks in emitted.items():
        assert all(t.agent_id == agent_id and isinstance(t.cost_idr, int) and t.cost_idr == 0 for t in tasks)
    assert {t.kind for t in emitted[BOOKING] + emitted[BILLING]} == {TaskKind.READ}
    assert {t.kind for t in emitted[FOLLOWUP]} == {TaskKind.CUSTOMER_MESSAGE}
    # evaluating again gives the same answer (the feeds are one-shot, so skip their own tasks)
    again = {a: r.triggers(env.ctx) for a, r in env.roles.items()}
    skip_feed = lambda tasks: [t.reason for t in tasks if t.payload.get("action") not in ("register", "payment")]
    assert {a: skip_feed(t) for a, t in again.items()} == {a: skip_feed(t) for a, t in emitted.items()}
    assert counts(conn) == before and len(env.llm.calls) == 0
    assert small and old  # both exist, so the approve and reminder triggers had something to look at


# ---------------------------------------------------------- feeds + wiring --


def test_simulated_booking_feed_is_deterministic_and_realistic(conn, clock):
    ctx = BusinessContext(conn, 1, clock)
    cust = customer(conn, clock)
    for resource in ("Kursi 1", "Kursi 2"):  # something to collide with
        for hour in (11, 13, 15):
            held = data.request_booking(conn, 1, cust, "Creambath", resource, at(hour, day=6), 60, clock)
            data.confirm_booking(conn, 1, held)
    a, b, other = (SimulatedBookingFeed(1, chance=0.5), SimulatedBookingFeed(1, chance=0.5), SimulatedBookingFeed(2, chance=0.5))
    names, services, overlapping, seen_other = set(), set(), 0, False
    for day in (6, 7, 8):
        for step in range(24 * 12):  # every 5 minutes of the whole day
            clock.set(jakarta(2025, 1, day, 0, 0) + timedelta(minutes=5 * step))
            local = clock.now().astimezone(JAKARTA)
            requests = a.poll(ctx)
            assert requests == b.poll(ctx)
            seen_other |= requests != other.poll(ctx)
            assert len(requests) <= 1
            if not 9 <= local.hour < 17:
                assert requests == []  # only during working hours
            for r in requests:
                assert a.poll(ctx) == []  # handed out once
                names.add(r["customer_name"])
                services.add((r["service"], r["duration_min"], r["price_idr"]))
                start = datetime.fromisoformat(r["starts_at"])
                end = start + timedelta(minutes=r["duration_min"])
                s_local = start.astimezone(JAKARTA)
                assert start >= clock.now() + timedelta(hours=1) and s_local.minute in (0, 30)
                assert s_local.hour >= 9 and end.astimezone(JAKARTA) <= s_local.replace(hour=17, minute=0)
                assert end.astimezone(JAKARTA).date() == s_local.date()
                assert r["channel"] in ("walk-in", "whatsapp") and r["phone"].startswith("0812")
                for held in data.list_bookings(conn, 1, status="confirmed"):
                    if held["resource"] == r["resource"] and start < start_of(held) + timedelta(minutes=held["duration_min"]) and start_of(held) < end:
                        overlapping += 1
                        break
    assert names and names <= set(CUSTOMER_NAMES) and len(names) > 3
    assert services and services <= {(s.name, s.duration_min, s.price_idr) for s in SERVICE_CATALOGUE}
    assert overlapping > 0 and seen_other


async def test_simulated_feed_in_a_running_office_does_not_duplicate_a_moved_request(conn, clock):
    cust = customer(conn, clock)
    for resource in ("Kursi 1", "Kursi 2"):  # the whole afternoon is taken, so any request is moved
        held = data.request_booking(conn, 1, cust, "Creambath", resource, at(11, day=6), 360, clock)
        data.confirm_booking(conn, 1, held)
    clock.set(jakarta(2025, 1, 6, 9, 0))
    feed = SimulatedBookingFeed(5, chance=1.0, overlap_chance=1.0)
    env = make_env(conn, clock, booking_feed=feed)
    for _ in range(6):  # six ticks inside the same 5-minute slot
        await env.heartbeat.tick()
    assert len(bookings(conn)) == 2 + 1  # one request, moved once, never registered again
    moved = bookings(conn)[2]
    assert moved["status"] == "confirmed" and start_of(moved) == jakarta(2025, 1, 7, 9)


def test_build_roles_maps_agent_rows_by_role_and_wires_feeds(conn, clock):
    conn.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (9, 1, 'barista', 'X', 9)")
    feed, pay = ListFeed(), ListPayments()
    roles = build_roles(
        conn, 1, clock, booking_feed=feed, payment_feed=pay,
        auto_approve_limit_idr=250_000, max_messages_per_day=2, overdue_days=5,
    )
    assert sorted(roles) == [BOOKING, BILLING, FOLLOWUP]
    assert isinstance(roles[BOOKING], BookingRole) and roles[BOOKING].feed is feed
    assert isinstance(roles[BILLING], BillingRole) and roles[BILLING].payment_feed is pay
    assert roles[BILLING].auto_approve_limit_idr == 250_000
    assert isinstance(roles[FOLLOWUP], FollowUpRole)
    assert (roles[FOLLOWUP].max_messages_per_day, roles[FOLLOWUP].overdue_days) == (2, 5)
    assert {a: r.agent_id for a, r in roles.items()} == {a: a for a in roles}
    assert [roles[a].name for a in sorted(roles)] == ["booking", "billing", "followup"]


def test_build_roles_without_feeds_simulates_nothing(conn, clock):
    roles = build_roles(conn, 1, clock)
    assert roles[BOOKING].feed is None and roles[BILLING].payment_feed is None
    assert roles[BILLING].auto_approve_limit_idr == 500_000
    assert (roles[FOLLOWUP].max_messages_per_day, roles[FOLLOWUP].overdue_days) == (5, 3)
    ctx = BusinessContext(conn, 1, clock)
    assert all(role.triggers(ctx) == [] for role in roles.values())


def test_registry_has_the_three_roles_and_roles_find_their_agent_without_build_roles(conn, clock):
    assert isinstance(JASA_ROLES, RoleRegistry)
    assert JASA_ROLES.names() == ["billing", "booking", "followup"]
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Potong Rambut", at(9))
    tasks = JASA_ROLES.create("billing").triggers(BusinessContext(conn, 1, clock))
    assert [t.agent_id for t in tasks] == [BILLING]


def test_rupiah_format():
    assert rupiah(0) == "Rp 0" and rupiah(1_250_000) == "Rp 1.250.000"


# ----------------------------------------------------------- 3-day full run --


def assert_invariants(conn, limit):
    # no two confirmed bookings overlap on one resource
    confirmed = [b for b in bookings(conn) if b["status"] == "confirmed" or b["status"] == "done"]
    by_resource = {}
    for b in confirmed:
        by_resource.setdefault(b["resource"], []).append((start_of(b), start_of(b) + timedelta(minutes=b["duration_min"])))
    for slots in by_resource.values():
        slots.sort()
        assert all(prev_end <= next_start for (_, prev_end), (next_start, _) in zip(slots, slots[1:]))
    # invoice statuses are consistent with who approved and when
    booking_status = {b["id"]: b["status"] for b in bookings(conn)}
    seen_booking = set()
    for i in invoices(conn):
        assert i["booking_id"] not in seen_booking and booking_status[i["booking_id"]] == "done"
        seen_booking.add(i["booking_id"])
        assert i["status"] in ("draft", "approved", "paid")
        if i["status"] == "draft":
            assert i["approved_by"] is None and i["paid_at"] is None
        else:
            assert i["approved_by"] == "policy" and i["amount_idr"] <= limit and i["approved_at"] is not None
        assert (i["status"] == "paid") == (i["paid_at"] is not None)
    # outbox is pending only
    assert {m["status"] for m in outbox(conn)} <= {"pending"}


async def test_three_day_simulation_keeps_its_invariants(conn, clock):
    limit, cap = 500_000, 3
    env = make_env(
        conn, clock, booking_feed=SimulatedBookingFeed(42, chance=0.5),
        payment_feed=SimulatedPaymentFeed(42), max_messages_per_day=cap, overdue_days=1,
    )
    start = jakarta(2025, 1, 6, 0, 0)  # a Monday
    results = []
    for step in range(3 * 96 + 8):  # 3 days of 15 minute ticks, plus a quiet night to settle
        clock.set(start + timedelta(minutes=15 * step))
        results += await env.heartbeat.tick()
        assert_invariants(conn, limit)

    # the office was alive
    statuses = {b["status"] for b in bookings(conn)}
    assert len(bookings(conn)) >= 15 and {"confirmed", "done"} <= statuses
    assert "requested" not in statuses  # everything registered was settled
    assert any(i["status"] == "paid" for i in invoices(conn)) and any(i["status"] == "draft" for i in invoices(conn))
    assert any(r.status == "pending" for r in results)
    assert any("usul" in r.message for r in results)  # at least one conflict was moved
    assert outbox(conn)

    # no failed work, short messages, and the model only wrote thank-you texts
    assert not [r for r in log(conn) if r["status"] == "failed"]
    assert all(len(r.message) <= MAX_MESSAGE_CHARS for r in results)
    # daily message cap per Jakarta day
    per_day = {}
    for r in log(conn):
        if r["action"] in ("Kirim pengingat", "Kirim terima kasih"):
            day = datetime.fromisoformat(r["created_at"]).astimezone(JAKARTA).date()
            per_day[day] = per_day.get(day, 0) + 1
    assert per_day and max(per_day.values()) <= cap and len(per_day) >= 2
    # a booking is thanked once and an invoice reminded at most once per 3 days
    thanks, reminders = {}, {}
    for r in log(conn):
        if r["action"] not in ("Kirim pengingat", "Kirim terima kasih") or r["status"] != "done":
            continue
        if "(booking #" in r["reason"]:
            key = r["reason"].split("(booking #")[1].split(")")[0]
            thanks[key] = thanks.get(key, 0) + 1
        else:
            key = r["reason"].split("(invoice #")[1].split(")")[0]
            reminders.setdefault(key, []).append(datetime.fromisoformat(r["created_at"]))
    assert thanks and set(thanks.values()) == {1} and reminders
    for times in reminders.values():
        assert all(b - a >= timedelta(days=3) for a, b in zip(times, times[1:]))


# ------------------------------------- demo hours and arrival rate (task 14.1) --
def _requests_over(feed, conn, clock, start, step_seconds, count):
    ctx = BusinessContext(conn, 1, clock)
    found = []
    for step in range(count):
        clock.set(start + timedelta(seconds=step_seconds * step))
        found += feed.poll(ctx)
    return found


def test_booking_feed_ignores_working_hours_with_demo_hours_and_keeps_them_by_default(conn, clock):
    night = jakarta(2025, 1, 8, 1, 0)  # 01:00 to 08:00, 5-minute slots
    kept = _requests_over(SimulatedBookingFeed(3, chance=0.5), conn, clock, night, 300, 7 * 12)
    demo = _requests_over(SimulatedBookingFeed(3, chance=0.5, demo_hours=True), conn, clock, night, 300, 7 * 12)
    assert kept == [] and len(demo) > 0
    again = _requests_over(SimulatedBookingFeed(3, chance=0.5, demo_hours=True), conn, clock, night, 300, 7 * 12)
    assert again == demo  # deterministic from the seed and the clock
    for r in demo:  # a request at night still asks for a time inside the working hours
        start = datetime.fromisoformat(r["starts_at"]).astimezone(JAKARTA)
        end = start + timedelta(minutes=r["duration_min"])
        assert 9 <= start.hour and end.hour * 60 + end.minute <= 17 * 60 and start.minute in (0, 30)


def test_a_short_interval_raises_the_arrival_chance_to_about_one_request_per_two_to_three_ticks(conn, clock):
    start = jakarta(2025, 1, 8, 22, 0)
    for interval in (8, 15, 30):
        feed = SimulatedBookingFeed(5, demo_hours=True, interval_seconds=interval)
        found = _requests_over(feed, conn, clock, start, interval, 300)
        assert 0.25 * 300 <= len(found) <= 0.55 * 300, (interval, len(found))  # about 0.4 per tick
    slow = _requests_over(SimulatedBookingFeed(5, demo_hours=True), conn, clock, start, 8, 300)
    assert len(slow) <= 3  # without the interval: one draw per 5-minute slot, as before


def test_arrival_chance_follows_the_interval_and_never_drops_below_the_base_chance():
    chance = lambda i: SimulatedBookingFeed(interval_seconds=i).effective_chance
    assert chance(None) == 0.15 and chance(300) == 0.15 and chance(900) == 0.15
    assert chance(8) == chance(30) == SimulatedBookingFeed.FAST_CHANCE
    assert 0.15 < chance(120) < SimulatedBookingFeed.FAST_CHANCE
    assert SimulatedBookingFeed(chance=0.9, interval_seconds=8).effective_chance == 0.9


async def test_the_calendar_does_not_fill_up_in_minutes_at_a_short_interval(conn, clock):
    clock.set(jakarta(2025, 1, 8, 22, 0))
    feed = SimulatedBookingFeed(8, demo_hours=True, interval_seconds=8)
    env = make_env(conn, clock, booking_feed=feed)
    for _ in range(400):  # about 53 minutes at --interval 8
        await env.heartbeat.tick()
        clock.advance(8)
    waiting = [b for b in bookings(conn) if b["status"] in ("requested", "confirmed")]
    assert 0 < len(waiting) <= SimulatedBookingFeed.CALENDAR_CAP
    assert not [b for b in bookings(conn) if b["status"] == "cancelled"]  # nothing was thrown away


# ------------------------------------------------------ labels and bubbles --
async def test_jasa_tasks_carry_friendly_labels_and_short_bubbles(conn, clock):
    cust = customer(conn, clock)
    data.request_booking(conn, 1, cust, "Creambath", "Kursi 2", at(13), 60, clock)  # confirm
    finished(conn, clock, cust, "Warna Rambut", at(9), 120, "Kursi 1")  # invoice (above the limit)
    old = approved(conn, clock, cust, 90_000, "owner", "Creambath")
    done = finished(conn, clock, cust, "Cuci dan Blow", at(8, day=5), 30, "Kursi 2")  # thanked the day after
    clock.advance(5 * 86400)
    env = make_env(conn, clock, booking_feed=ListFeed(req(name="Siti Aminah", start=at(13, day=6))),
                   payment_feed=ListPayments({"invoice_id": old}))
    tasks = {}
    for role in env.roles.values():
        for task in role.triggers(env.ctx):
            tasks.setdefault(task.payload["action"], task)
    labels = {action: t.label for action, t in tasks.items()}
    assert labels["register"] == "Catat permintaan" and labels["confirm"] == "Konfirmasi booking"
    assert labels["invoice"] == "Buat invoice" and labels["payment"] == "Catat pembayaran"
    assert labels["payment_reminder"] == "Kirim pengingat" and labels["thank_you"] == "Kirim terima kasih"
    for task in tasks.values():
        assert 0 < len(task.bubble) <= 45 and task.bubble != task.reason, task.payload["action"]
    assert tasks["register"].bubble == "Catat permintaan Siti" and done


async def test_the_activity_feed_reads_friendly_actions_and_never_the_task_kind(conn, clock):
    cust = customer(conn, clock)
    data.request_booking(conn, 1, cust, "Creambath", "Kursi 2", at(13), 60, clock)
    finished(conn, clock, cust, "Cuci dan Blow", at(8), 30, "Kursi 2")
    clock.advance(1000)
    env = make_env(conn, clock, booking_feed=ListFeed(req(start=at(15))))
    await env.heartbeat.tick()
    actions = {r["action"] for r in log(conn)}
    assert {"Catat permintaan", "Konfirmasi booking", "Buat invoice"} <= actions
    assert not actions & {"read", "draft", "spend", "customer_message"}


async def test_a_waiting_invoice_shows_a_short_waiting_bubble(conn, clock):
    cust = customer(conn, clock)
    finished(conn, clock, cust, "Warna Rambut", at(9), 120, "Kursi 1")
    env = make_env(conn, clock)
    [result] = await env.heartbeat.tick()
    assert result.status == "pending" and result.bubble == "Invoice #1 menunggu OK pemilik"


async def test_a_rejected_message_row_says_what_was_rejected(conn, clock):
    cust = customer(conn, clock)
    approved(conn, clock, cust, 650_000, "owner", "Warna Rambut")
    clock.advance(5 * 86400)
    set_policy(conn, 1, 100_000, ["customer_message"])  # the owner blocked messages
    env = make_env(conn, clock)
    pause_billing(conn)
    await env.heartbeat.tick()
    [row] = [r for r in log(conn) if r["status"] == "rejected"]
    assert row["action"] == "Kirim pengingat"
    assert row["reason"].startswith("Kirim pengingat ditolak: jenis aksi ini diblokir pemilik. Tugas: Pengingat bayar Budi Santoso")


def test_the_jasa_template_defaults_to_the_demo_and_real_hours_restore_the_limits(conn, clock):
    from apps.kantor_jasa.template import build_jasa_roles

    feed = build_jasa_roles(conn, 1, clock)[BOOKING].feed
    assert feed.demo_hours is True and feed.interval_seconds is None
    real = build_jasa_roles(conn, 1, clock, real_hours=True, interval_seconds=10)[BOOKING].feed
    assert real.demo_hours is False and real.interval_seconds == 10
