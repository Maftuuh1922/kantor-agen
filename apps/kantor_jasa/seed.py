"""Demo data for Kantor Jasa (Req 10.1): customers, a service history and tomorrow's agenda.

A fresh Jasa office (a salon in the demo) starts with 8 customers, the service
catalogue of ``SimulatedBookingFeed`` and about two weeks of history, so the demo
runs without manual data entry. Everything is written through the data layer
(``add_customer``, ``request_booking``, ``confirm_booking``, ``complete_booking``,
``create_invoice``, ``approve_invoice``, ``mark_paid``). There is no raw SQL on the
money tables, and the invoice triggers stay in force.

What the data is built to show
------------------------------
* Done services with a PAID invoice (some approved by the policy, one large one
  approved by the owner).
* Two done services whose invoice is APPROVED but unpaid for 8 and 10 days: the
  follow-up agent queues a payment reminder for each on its first tick.
* One done service from yesterday WITHOUT an invoice (a Warna Rambut, above the
  auto-approve limit): billing drafts it on its first tick and leaves it for the
  owner (the amber "waiting for the owner" state). Follow-up thanks the customer.
* One large DRAFT invoice (Rp 650.000, above the limit) the owner has not seen yet.
* Two confirmed bookings tomorrow, and two requested ones: one is free (booking
  confirms it) and one clashes with a confirmed slot (booking proposes the next
  free slot).

The unpaid invoices must stay unpaid in the live demo. The app's
``SimulatedPaymentFeed`` pays an invoice after a delay that depends on its seed
and its id, so ``DEMO_PAYMENT_SEED`` is a feed seed for which the seeded unpaid
invoices are customers who never pay. ``tests/test_jasa_seed.py`` checks it.

Dates are relative to ``clock.today()`` (Asia/Jakarta), written with a settable
past clock so ``created_at``, ``approved_at`` and ``paid_at`` lie in their own
days. The plan is fixed, so every Jasa office starts the same.

Known limit: if this function fails half way, ``bootstrap`` cannot remove the
business again, because approved invoices cannot be deleted and the ``invoice``
table has no ON DELETE CASCADE. The plan is deterministic and tested, so this only
happens on a disk or database fault.

Depends only on ``skeleton.*`` and this app's own modules; the skeleton never
imports this module.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from apps.kantor_jasa import data
from apps.kantor_jasa.roles import (
    DEFAULT_AUTO_APPROVE_LIMIT_IDR,
    DEFAULT_RESOURCES,
    SERVICE_CATALOGUE,
    _phone_for,
)
from apps.kantor_jasa.schema import init_jasa_schema
from skeleton.core.clock import JAKARTA, Clock, FakeClock

# Seed of the live app's SimulatedPaymentFeed. With it the invoices that the plan
# leaves unpaid (ids 1 and 3) are never paid, so their reminders stay due.
DEMO_PAYMENT_SEED = 84

# Customers of the demo salon, by index. All of them are also in the simulated
# feed's name list, so a returning WhatsApp customer is found by phone number.
SEED_CUSTOMERS: tuple[str, ...] = (
    "Budi Santoso",
    "Siti Aminah",
    "Eko Prasetyo",
    "Sri Wahyuni",
    "Ani Rahayu",
    "Hendra Gunawan",
    "Lina Kartika",
    "Maya Puspita",
)

PRICES: dict[str, int] = {s.name: s.price_idr for s in SERVICE_CATALOGUE}
DURATIONS: dict[str, int] = {s.name: s.duration_min for s in SERVICE_CATALOGUE}

# How a past (done) service ended.
PAID = "paid"  # approved and paid
UNPAID = "unpaid"  # approved, not paid yet
DRAFT = "draft"  # invoice drafted, not approved
UNBILLED = "unbilled"  # no invoice at all
# How a future booking stands.
CONFIRMED = "confirmed"
REQUESTED = "requested"

PAY_AFTER = timedelta(hours=2)
OWNER_PAY_AFTER = timedelta(hours=30)
REQUEST_LEAD = timedelta(hours=20)  # a customer asks about a day before the visit
HISTORY_START_DAYS = 12  # customers were added this many days ago


@dataclass(frozen=True)
class PlannedBooking:
    day: int  # offset in days from today (Asia/Jakarta): negative is the past
    hour: int
    minute: int
    customer: int  # index in SEED_CUSTOMERS
    service: str  # a name of SERVICE_CATALOGUE
    resource: str
    outcome: str  # PAID, UNPAID, DRAFT, UNBILLED, CONFIRMED or REQUESTED
    owner_approved: bool = False  # an invoice above the limit the owner approved by hand


K1, K2 = DEFAULT_RESOURCES[0], DEFAULT_RESOURCES[1]

# Oldest first. The invoice ids follow this order (ids 1 to 10 for the rows that have an invoice).
PAST: tuple[PlannedBooking, ...] = (
    PlannedBooking(-10, 10, 0, 1, "Creambath", K2, UNPAID),  # invoice 1: 10 days unpaid
    PlannedBooking(-9, 9, 0, 0, "Potong Rambut", K1, PAID),
    PlannedBooking(-8, 10, 30, 2, "Potong Rambut", K1, UNPAID),  # invoice 3: 8 days unpaid
    PlannedBooking(-8, 13, 0, 3, "Cuci dan Blow", K2, PAID),
    PlannedBooking(-7, 9, 30, 4, "Warna Rambut", K1, PAID, owner_approved=True),
    PlannedBooking(-6, 14, 0, 5, "Cuci dan Blow", K2, PAID),
    PlannedBooking(-5, 11, 0, 6, "Potong Rambut", K1, PAID),
    PlannedBooking(-4, 15, 0, 7, "Creambath", K2, PAID),
    PlannedBooking(-3, 12, 0, 3, "Warna Rambut", K1, DRAFT),  # the big draft waiting for the owner
    PlannedBooking(-1, 13, 0, 4, "Warna Rambut", K1, UNBILLED),  # billing reacts on its first tick
    PlannedBooking(-1, 15, 30, 6, "Potong Rambut", K2, PAID),
)

# Tomorrow. "Potong Rambut" at 10:30 clashes with the confirmed 10:00 one on the same chair.
TOMORROW: tuple[PlannedBooking, ...] = (
    PlannedBooking(1, 10, 0, 1, "Potong Rambut", K1, CONFIRMED),
    PlannedBooking(1, 11, 0, 5, "Creambath", K2, CONFIRMED),
    PlannedBooking(1, 13, 0, 2, "Cuci dan Blow", K1, REQUESTED),
    PlannedBooking(1, 10, 30, 6, "Potong Rambut", K1, REQUESTED),
)


def _at(today: date, day: int, hour: int, minute: int) -> datetime:
    return datetime.combine(today + timedelta(days=day), time(hour, minute), tzinfo=JAKARTA)


def _add_past_service(
    conn: sqlite3.Connection, business_id: int, customer_id: int, plan: PlannedBooking, start: datetime
) -> None:
    """One finished service and whatever happened to its invoice."""
    duration = DURATIONS[plan.service]
    end = start + timedelta(minutes=duration)
    past = FakeClock(start - REQUEST_LEAD)
    booking_id = data.request_booking(
        conn, business_id, customer_id, plan.service, plan.resource, start, duration, past
    )
    data.confirm_booking(conn, business_id, booking_id)
    data.complete_booking(conn, business_id, booking_id)
    if plan.outcome == UNBILLED:
        return
    amount = PRICES[plan.service]
    past.set(end + timedelta(minutes=5))
    invoice_id = data.create_invoice(
        conn, business_id, customer_id, plan.service, amount, booking_id, past
    )
    if plan.outcome == DRAFT:
        return
    past.set(end + timedelta(minutes=10))
    owner = plan.owner_approved or amount > DEFAULT_AUTO_APPROVE_LIMIT_IDR
    data.approve_invoice(conn, business_id, invoice_id, "owner" if owner else "policy", past)
    if plan.outcome == PAID:
        past.set(end + (OWNER_PAY_AFTER if owner else PAY_AFTER))
        data.mark_paid(conn, business_id, invoice_id, past)


def seed_jasa(conn: sqlite3.Connection, business_id: int, clock: Clock) -> None:
    """Create the Jasa tables, then add customers, history and tomorrow's agenda.

    Does nothing when the business already has customers. Used as ``template.seed``,
    so the tables exist before the first row is written (Req 10.1).
    """
    init_jasa_schema(conn)
    if data.list_customers(conn, business_id):
        return
    today = date.fromisoformat(clock.today())

    joined = FakeClock(_at(today, -HISTORY_START_DAYS, 9, 0))
    customer_ids = [
        data.add_customer(conn, business_id, name, _phone_for(name), joined) for name in SEED_CUSTOMERS
    ]

    for plan in PAST:
        _add_past_service(
            conn, business_id, customer_ids[plan.customer], plan, _at(today, plan.day, plan.hour, plan.minute)
        )

    for plan in TOMORROW:
        start = _at(today, plan.day, plan.hour, plan.minute)
        booking_id = data.request_booking(
            conn, business_id, customer_ids[plan.customer], plan.service, plan.resource,
            start, DURATIONS[plan.service], clock,
        )
        if plan.outcome == CONFIRMED:
            data.confirm_booking(conn, business_id, booking_id)
