"""Kantor Jasa roles: booking, billing and follow-up (Req 1.2, 3.2, 3.3, 5.4).

Every role is a :class:`BaseRole`. ``triggers`` is pure rule logic: it reads the
database and the clock, makes no model call and writes nothing (Req 3.3). Model
text is added later by the Manager, only for ``customer_message`` tasks that
come without a body. All money is integer rupiah.

Who does what
-------------
* **Booking** takes requests from a pluggable :class:`BookingFeed` (walk-in or
  WhatsApp style) and registers them as ``requested`` bookings (creating the
  customer when new). Every ``requested`` booking is then confirmed when its
  slot is free. If the slot is taken (``BookingConflict``), it is not
  silently dropped: the booking is moved to the first free slot
  (``next_free_slot``) and stays ``requested``, so the next tick confirms it
  there. A request whose start is already past, or outside working hours, gets
  the same treatment. Confirmed bookings whose end time has passed become
  ``done``. All tasks are kind ``read``.
* **Billing** creates one draft invoice per ``done`` booking at the catalogue
  price. An amount at or below the owner-set limit
  (``auto_approve_limit_idr``, default Rp 500.000) is approved by ``policy``.
  A larger one stays ``draft`` and the task result is ``pending`` (the owner
  decides, the office shows the amber marker). A pluggable
  :class:`PaymentFeed` reports payments; the role marks those invoices paid.
  Billing only moves invoices through ``approve_invoice`` and ``mark_paid``. It
  never edits an approved invoice (the database triggers forbid it anyway).
* **Follow-up** queues two kinds of WhatsApp message in the outbox (never sent,
  Req 5.4): a thank-you the day after a completed service, and a payment
  reminder for overdue invoices (at most one per invoice per 3 days). At most
  ``max_messages_per_day`` messages are queued per day.

Who writes the text
-------------------
* Payment reminders carry a rule-based template in ``payload["body"]``, so the
  amount and the invoice number are exact and the Manager never drafts them.
* Thank-you messages carry no body, so the Manager drafts the text with the
  strong model. The Manager writes the outbox row before the role runs and, when
  the model fails, fills it with a neutral default. In that case ``run``
  replaces that default row with the role's template (see ``_FollowUp._thanks``).

"Already done" without module state
-----------------------------------
The ``outbox`` table has no timestamp and no reference column, so it cannot say
which booking or invoice a message was for, or when it was queued. The roles
therefore put a marker in the task reason, ``(booking #12)`` for a thank-you and
``(invoice #7)`` for a reminder. The Manager copies the reason into the activity
row of the queued message ("... (queued in outbox)"), and the follow-up role
reads those rows back (marker plus ``created_at``) to decide what was already
queued, which day it was, and how many messages went out today. Activity rows
of action ``customer_message`` are therefore part of this role's behavior and
must not be removed. The booking and billing roles need no such rows: the
booking and invoice tables carry their own state.

Depends only on ``skeleton.*`` and this app's own data layer; the skeleton never
imports this module.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import zlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, NamedTuple, Protocol, runtime_checkable

from apps.kantor_jasa import data
from apps.kantor_jasa.schema import MAX_DURATION_MIN
from skeleton.core.clock import JAKARTA, Clock
from skeleton.runtime.manager import DEFAULT_BODY_TEMPLATE, DEFAULT_SENDER_NAME
from skeleton.runtime.models import ActionResult, BaseRole, BusinessContext, Role, Task, TaskKind
from skeleton.runtime.registry import RoleRegistry

logger = logging.getLogger(__name__)

ROLE_BOOKING = "booking"
ROLE_BILLING = "billing"
ROLE_FOLLOWUP = "followup"

DEFAULT_AUTO_APPROVE_LIMIT_IDR = 500_000
DEFAULT_OVERDUE_DAYS = 3
DEFAULT_MAX_MESSAGES_PER_DAY = 5
REMINDER_INTERVAL = timedelta(days=3)
THANKS_MAX_AGE_DAYS = 2  # thank on the day after, or one day later if the cap held it back
MESSAGE_CHANNEL = "whatsapp"
BUBBLE_MAX_CHARS = 60
DEFAULT_RESOURCES = ("Kursi 1", "Kursi 2")

ACTION_CUSTOMER_MESSAGE = TaskKind.CUSTOMER_MESSAGE  # the action the Manager logs for messages

# Payload "action" values, used to route a task to its handler in ``run``.
ACT_REGISTER = "register"
ACT_CONFIRM = "confirm"
ACT_COMPLETE = "complete"
ACT_INVOICE = "invoice"
ACT_APPROVE = "approve"
ACT_PAYMENT = "payment"
ACT_THANKS = "thank_you"
ACT_REMINDER = "payment_reminder"


class Service(NamedTuple):
    """One line of the service catalogue: name, duration and price (integer rupiah)."""

    name: str
    duration_min: int
    price_idr: int


SERVICE_CATALOGUE: tuple[Service, ...] = (
    Service("Potong Rambut", 45, 50_000),
    Service("Cuci dan Blow", 30, 40_000),
    Service("Creambath", 60, 120_000),
    Service("Warna Rambut", 120, 650_000),  # above the default auto-approve limit
)

CUSTOMER_NAMES: tuple[str, ...] = (
    "Budi Santoso", "Siti Aminah", "Agus Wijaya", "Dewi Lestari", "Rina Marlina",
    "Joko Susilo", "Sri Wahyuni", "Eko Prasetyo", "Ani Rahayu", "Hendra Gunawan",
    "Lina Kartika", "Bambang Hartono", "Yuni Safitri", "Dedi Kurniawan", "Maya Puspita",
    "Rudi Hermawan", "Fitri Handayani", "Andi Saputra", "Putri Ayu", "Wawan Setiawan",
)


# ------------------------------------------------------------------ helpers --


def rupiah(amount: int) -> str:
    """Format integer rupiah the Indonesian way: ``Rp 54.000``."""
    return "Rp " + f"{amount:,}".replace(",", ".")


def _short(text: str, limit: int = BUBBLE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(moment: datetime) -> str:
    # Same fixed format as skeleton/log/activity.py, so string order is time order.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _day_bounds(day: str) -> tuple[str, str]:
    """UTC ISO range ``[start, end)`` of one Asia/Jakarta calendar day."""
    local_day = date.fromisoformat(day)
    start = datetime.combine(local_day, time.min, tzinfo=JAKARTA)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=JAKARTA)
    return _iso(start), _iso(end)


def _local(moment: datetime) -> datetime:
    return moment.astimezone(JAKARTA)


def _when(moment: datetime) -> str:
    return f"{_local(moment):%d/%m %H:%M}"


def _find_agent(ctx: BusinessContext, role_name: str) -> int | None:
    row = ctx.conn.execute(
        "SELECT id FROM agent WHERE business_id = ? AND role = ? ORDER BY id LIMIT 1",
        (ctx.business_id, role_name),
    ).fetchone()
    return int(row[0]) if row else None


def _booking_end(booking: dict) -> datetime:
    return datetime.fromisoformat(booking["starts_at"]) + timedelta(minutes=booking["duration_min"])


def _customer_name(ctx: BusinessContext, customer_id: int) -> str:
    customer = data.get_customer(ctx.conn, ctx.business_id, customer_id)
    return customer["name"] if customer else f"pelanggan #{customer_id}"


def _business_name(ctx: BusinessContext) -> str:
    row = ctx.conn.execute("SELECT name FROM business WHERE id = ?", (ctx.business_id,)).fetchone()
    return row[0] if row else "kami"


def _price_key(service: str) -> str:
    return service.strip().casefold()


class _JasaRole(BaseRole):
    """Shared plumbing: which agent row this role instance belongs to, and routing."""

    def __init__(self, agent_id: int | None = None) -> None:
        # None means "look the agent up by role name" (used by RoleRegistry.create).
        self.agent_id = agent_id

    def _me(self, ctx: BusinessContext) -> int | None:
        return self.agent_id if self.agent_id is not None else _find_agent(ctx, self.name)

    def run(self, task: Task, ctx: BusinessContext) -> ActionResult:
        handler = self._handlers().get(task.payload.get("action"))
        if handler is None:
            return ActionResult(status="info", message=f"{self.name}: tidak ada yang dikerjakan")
        return handler(task, ctx)

    def _handlers(self) -> dict[str, Any]:
        return {}


# ------------------------------------------------------------------- feeds --


@runtime_checkable
class BookingFeed(Protocol):
    """Source of booking requests for the booking role (WhatsApp, walk-in, a simulator...)."""

    def poll(self, ctx: BusinessContext) -> list[dict]:
        """Return new requests as dicts. Must not write to the database.

        Keys: ``customer_name``, ``service``, ``resource``, ``starts_at`` (ISO-8601
        with a UTC offset), ``duration_min`` (int); optional ``phone``, ``channel``.
        """
        ...


@runtime_checkable
class PaymentFeed(Protocol):
    """Source of payments for the billing role (a bank, a payment gateway, a simulator...)."""

    def poll(self, ctx: BusinessContext) -> list[dict]:
        """Return payments as ``{"invoice_id": int}`` dicts. Must not write to the database."""
        ...


def _ceil_to_grid(moment: datetime) -> datetime:
    """Round a local time up to the 30-minute grid of ``next_free_slot``."""
    floor = moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)
    return floor if floor == moment else floor + data.SLOT_STEP


def _phone_for(name: str) -> str:
    """A stable fake phone number per name, so the same name is the same customer."""
    return f"0812{zlib.crc32(name.encode('utf-8')) % 10**8:08d}"


class SimulatedBookingFeed:
    """Deterministic pseudo walk-in and WhatsApp requests for the demo.

    A poll returns 0 or 1 request, and only during working hours (default 09:00
    to 17:00 Jakarta). The generator is seeded from ``seed``, the Jakarta day,
    the hour and the 5-minute slot, so the same clock gives the same request
    (reproducible) while different ticks differ. A request asks for a start
    time on the 30-minute grid, at least one hour ahead, today or tomorrow, and
    always fits the working hours. In about ``overlap_chance`` of the requests
    it asks for a time that overlaps an already confirmed booking, so the
    conflict path gets exercised. Polling reads the database and writes nothing.
    A request is handed out once: polling the same 5-minute slot again returns
    nothing, while a new feed with the same seed repeats the same requests.
    """

    SLOT_MINUTES = 5
    LEAD = timedelta(hours=1)

    def __init__(
        self,
        seed: int = 0,
        *,
        catalogue: Sequence[Service] = SERVICE_CATALOGUE,
        resources: Sequence[str] = DEFAULT_RESOURCES,
        hours: tuple[int, int] = data.DEFAULT_HOURS,
        chance: float = 0.15,
        overlap_chance: float = 0.3,
    ) -> None:
        if not catalogue or not resources:
            raise ValueError("catalogue and resources must not be empty")
        self.seed = seed
        self.catalogue = tuple(catalogue)
        self.resources = tuple(resources)
        self.hours = hours
        self.chance = chance
        self.overlap_chance = overlap_chance
        # Key of the last slot that produced a request. A feed hands each request
        # out once, like a real inbox: the heartbeat polls more often than every
        # 5 minutes, and a booking that was moved to another slot or cancelled
        # must not be registered again by the next poll of the same slot.
        self._served_key: str | None = None

    def poll(self, ctx: BusinessContext) -> list[dict]:
        now = _local(ctx.clock.now())
        open_hour, close_hour = self.hours
        if not open_hour <= now.hour < close_hour:
            return []
        key = f"{self.seed}|booking|{now.date().isoformat()}|{now.hour}|{now.minute // self.SLOT_MINUTES}"
        if key == self._served_key:
            return []
        rng = random.Random(key)
        if rng.random() >= self.chance:
            return []
        name = rng.choice(CUSTOMER_NAMES)
        channel = rng.choice(("walk-in", "whatsapp"))
        service = rng.choice(self.catalogue)
        resource = rng.choice(self.resources)
        start = self._pick_start(rng, now, service)
        if start is None:
            return []
        if rng.random() < self.overlap_chance:
            clash = self._overlapping_start(ctx, rng, now, service)
            if clash is not None:
                resource, start = clash
        self._served_key = key
        return [
            {
                "customer_name": name,
                "phone": _phone_for(name),
                "service": service.name,
                "duration_min": service.duration_min,
                "price_idr": service.price_idr,
                "resource": resource,
                "starts_at": start.isoformat(),
                "channel": channel,
            }
        ]

    # ------------------------------------------------------------ helpers --

    def _day_starts(self, day: date, duration: timedelta, earliest: datetime) -> list[datetime]:
        """Grid starts on ``day`` that fit the working hours and are not before ``earliest``."""
        midnight = datetime.combine(day, time.min, tzinfo=JAKARTA)
        open_at = midnight + timedelta(hours=self.hours[0])
        close_at = midnight + timedelta(hours=self.hours[1])
        starts = []
        start = max(open_at, earliest)
        while start + duration <= close_at:
            starts.append(start)
            start += data.SLOT_STEP
        return starts

    def _pick_start(self, rng: random.Random, now: datetime, service: Service) -> datetime | None:
        duration = timedelta(minutes=service.duration_min)
        earliest = _ceil_to_grid(now + self.LEAD)
        today = self._day_starts(now.date(), duration, earliest)
        tomorrow = self._day_starts(now.date() + timedelta(days=1), duration, earliest)
        pool = today if today and rng.random() < 0.7 else (tomorrow or today)
        return rng.choice(pool) if pool else None

    def _overlapping_start(
        self,
        ctx: BusinessContext,
        rng: random.Random,
        now: datetime,
        service: Service,
    ) -> tuple[str, datetime] | None:
        """Resource and start of a slot that overlaps an upcoming confirmed booking, or None."""
        duration = timedelta(minutes=service.duration_min)
        earliest = _ceil_to_grid(now + self.LEAD)
        upcoming = []
        for offset in (0, 1):
            day = now.date() + timedelta(days=offset)
            upcoming += [
                b
                for b in data.list_bookings(ctx.conn, ctx.business_id, day=day, status="confirmed")
                if _local(_booking_end(b)) > earliest
            ]
        if not upcoming:
            return None
        target = rng.choice(upcoming)
        target_start = _local(datetime.fromisoformat(target["starts_at"]))
        shift = data.SLOT_STEP if target["duration_min"] >= 60 and rng.random() < 0.5 else timedelta(0)
        start = max(target_start + shift, earliest)
        midnight = datetime.combine(start.date(), time.min, tzinfo=JAKARTA)
        fits = start + duration <= midnight + timedelta(hours=self.hours[1])
        if not fits or start >= _local(_booking_end(target)):
            return None
        return target["resource"], start


class SimulatedPaymentFeed:
    """Deterministic pseudo payments: customers pay approved invoices after a delay.

    The delay of an invoice depends only on ``seed`` and the invoice id: 60% pay
    within 1 to 36 hours of approval, 25% pay late (4 to 8 days), 15% never pay,
    so overdue reminders have something to chase. An invoice is reported once the
    clock passes ``approved_at + delay``. Polling reads the database and writes nothing.
    """

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def delay_for(self, invoice_id: int) -> timedelta | None:
        """How long after approval the invoice is paid, or ``None`` when it never is."""
        rng = random.Random(f"{self.seed}|payment|{invoice_id}")
        roll = rng.random()
        if roll < 0.60:
            return timedelta(minutes=rng.randint(60, 36 * 60))
        if roll < 0.85:
            return timedelta(days=rng.randint(4, 8), hours=rng.randint(0, 23))
        return None

    def poll(self, ctx: BusinessContext) -> list[dict]:
        now = ctx.clock.now()
        due = []
        for invoice in data.list_invoices(ctx.conn, ctx.business_id, status="approved"):
            delay = self.delay_for(invoice["id"])
            if delay is None:
                continue
            if now >= datetime.fromisoformat(invoice["approved_at"]) + delay:
                due.append({"invoice_id": invoice["id"]})
        return due


# ------------------------------------------------------------------ booking --


def _valid_request(request: Any) -> dict | None:
    """Normalized copy of a feed request, or None when it is unusable."""
    if not isinstance(request, dict):
        return None
    texts = []
    for key in ("customer_name", "service", "resource"):
        value = request.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        texts.append(value.strip())
    duration = request.get("duration_min")
    if not _is_int(duration) or not 1 <= duration <= MAX_DURATION_MIN:
        return None
    try:
        start = datetime.fromisoformat(request.get("starts_at"))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None or start.utcoffset() is None:
        return None
    phone = request.get("phone")
    if phone is not None and (not isinstance(phone, str) or not phone.strip()):
        phone = None
    return {
        "customer_name": texts[0],
        "phone": phone.strip() if phone else None,
        "service": texts[1],
        "resource": texts[2],
        "duration_min": duration,
        "starts_at": start.isoformat(),
        "channel": str(request.get("channel") or "walk-in"),
    }


class BookingRole(_JasaRole):
    """Registers requests from a feed, confirms them, reschedules conflicts, completes them."""

    name = ROLE_BOOKING

    def __init__(
        self,
        agent_id: int | None = None,
        feed: BookingFeed | None = None,
        hours: tuple[int, int] = data.DEFAULT_HOURS,
    ) -> None:
        super().__init__(agent_id)
        self.feed = feed
        self.hours = hours

    # ----------------------------------------------------------- triggers --

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None:
            return []
        return self._register_tasks(ctx, me) + self._confirm_tasks(ctx, me) + self._complete_tasks(ctx, me)

    def _register_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        if self.feed is None:
            return []
        tasks = []
        for raw in self.feed.poll(ctx):
            request = _valid_request(raw)
            if request is None:
                continue
            when = _when(datetime.fromisoformat(request["starts_at"]))
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={"action": ACT_REGISTER, **request},
                    cost_idr=0,
                    reason=f"Catat permintaan: {request['customer_name']}, {request['service']} {when}",
                )
            )
        return tasks

    def _confirm_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        tasks = []
        for booking in data.list_bookings(ctx.conn, ctx.business_id, status="requested"):
            when = _when(datetime.fromisoformat(booking["starts_at"]))
            who = _customer_name(ctx, booking["customer_id"])
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={"action": ACT_CONFIRM, "booking_id": booking["id"]},
                    cost_idr=0,
                    reason=f"Konfirmasi booking #{booking['id']}: {who}, {booking['service']} {when}",
                )
            )
        return tasks

    def _complete_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        now = ctx.clock.now()
        tasks = []
        for booking in data.list_bookings(ctx.conn, ctx.business_id, status="confirmed"):
            if _booking_end(booking) > now:
                continue
            who = _customer_name(ctx, booking["customer_id"])
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={"action": ACT_COMPLETE, "booking_id": booking["id"]},
                    cost_idr=0,
                    reason=f"Selesaikan booking #{booking['id']}: {who}, {booking['service']}",
                )
            )
        return tasks

    # ----------------------------------------------------------- handlers --

    def _handlers(self) -> dict[str, Any]:
        return {ACT_REGISTER: self._register, ACT_CONFIRM: self._confirm, ACT_COMPLETE: self._complete}

    @staticmethod
    def _customer_for(ctx: BusinessContext, name: str, phone: str | None) -> int:
        """Id of the customer with this phone (or name when there is no phone); created if new."""
        for customer in data.list_customers(ctx.conn, ctx.business_id):
            if phone is not None:
                same = customer["phone"] == phone
            else:
                same = customer["name"].casefold() == name.casefold()
            if same:
                return customer["id"]
        return data.add_customer(ctx.conn, ctx.business_id, name, phone, ctx.clock)

    @staticmethod
    def _find_duplicate(
        ctx: BusinessContext, customer_id: int, service: str, resource: str, start: datetime
    ) -> dict | None:
        for booking in data.list_bookings(ctx.conn, ctx.business_id, day=_local(start).date()):
            if (
                booking["customer_id"] == customer_id
                and booking["service"] == service
                and booking["resource"] == resource
                and booking["status"] != "cancelled"
                and datetime.fromisoformat(booking["starts_at"]) == start
            ):
                return booking
        return None

    def _register(self, task: Task, ctx: BusinessContext) -> ActionResult:
        request = _valid_request(task.payload)
        if request is None:
            return ActionResult(status="failed", message="Gagal: permintaan booking tidak valid")
        start = datetime.fromisoformat(request["starts_at"])
        try:
            customer_id = self._customer_for(ctx, request["customer_name"], request["phone"])
            duplicate = self._find_duplicate(ctx, customer_id, request["service"], request["resource"], start)
            if duplicate is not None:
                return ActionResult(
                    status="info",
                    message=_short(f"Booking #{duplicate['id']} sudah tercatat"),
                    data={"booking_id": duplicate["id"]},
                )
            booking_id = data.request_booking(
                ctx.conn, ctx.business_id, customer_id, request["service"], request["resource"],
                start, request["duration_min"], ctx.clock,
            )
        except data.JasaError as exc:
            return ActionResult(status="failed", message=_short(f"Gagal catat booking: {exc}"))
        return ActionResult(
            status="done",
            message=_short(f"Permintaan {request['customer_name']} masuk, {_when(start)}"),
            data={"booking_id": booking_id, "customer_id": customer_id},
        )

    def _fits_hours(self, start: datetime, duration_min: int) -> bool:
        local = _local(start)
        midnight = datetime.combine(local.date(), time.min, tzinfo=JAKARTA)
        opens = midnight + timedelta(hours=self.hours[0])
        closes = midnight + timedelta(hours=self.hours[1])
        return opens <= local and local + timedelta(minutes=duration_min) <= closes

    def _confirm(self, task: Task, ctx: BusinessContext) -> ActionResult:
        booking_id = task.payload.get("booking_id")
        booking = data.get_booking(ctx.conn, ctx.business_id, booking_id) if _is_int(booking_id) else None
        if booking is None:
            return ActionResult(status="failed", message="Gagal: booking tidak ditemukan")
        if booking["status"] != "requested":
            return ActionResult(status="info", message=_short(f"Booking #{booking_id} sudah diproses"))
        start = datetime.fromisoformat(booking["starts_at"])
        now = ctx.clock.now()
        if start < now:
            return self._reschedule(ctx, booking, "waktu sudah lewat")
        if not self._fits_hours(start, booking["duration_min"]):
            return self._reschedule(ctx, booking, "di luar jam kerja")
        try:
            data.confirm_booking(ctx.conn, ctx.business_id, booking_id)
        except data.BookingConflict:
            return self._reschedule(ctx, booking, "bentrok")
        except data.BookingStateError:
            return ActionResult(status="info", message=_short(f"Booking #{booking_id} sudah diproses"))
        who = _customer_name(ctx, booking["customer_id"])
        return ActionResult(
            status="done",
            message=_short(f"Booking #{booking_id} {who} dikonfirmasi {_when(start)}"),
            data={"booking_id": booking_id},
        )

    def _reschedule(self, ctx: BusinessContext, booking: dict, why: str) -> ActionResult:
        """Propose the first free slot and re-request the booking there (it stays ``requested``)."""
        booking_id = booking["id"]
        start = datetime.fromisoformat(booking["starts_at"])
        slot = data.next_free_slot(
            ctx.conn, ctx.business_id, booking["resource"], booking["duration_min"],
            max(start, ctx.clock.now()), self.hours,
        )
        if slot is None:
            try:
                data.cancel_booking(ctx.conn, ctx.business_id, booking_id)
            except data.JasaError as exc:
                return ActionResult(status="failed", message=_short(f"Gagal: {exc}"))
            return ActionResult(
                status="done",
                message=_short(f"Booking #{booking_id} batal, tidak ada slot kosong"),
                data={"booking_id": booking_id, "cancelled": True},
            )
        try:
            data.reschedule_booking(ctx.conn, ctx.business_id, booking_id, slot)
        except data.JasaError as exc:
            return ActionResult(status="failed", message=_short(f"Gagal jadwal ulang: {exc}"))
        return ActionResult(
            status="done",
            message=_short(f"Booking #{booking_id} {why}, usul {_when(slot)}"),
            data={"booking_id": booking_id, "proposed_start": slot.isoformat(), "reason": why},
        )

    @staticmethod
    def _complete(task: Task, ctx: BusinessContext) -> ActionResult:
        booking_id = task.payload.get("booking_id")
        booking = data.get_booking(ctx.conn, ctx.business_id, booking_id) if _is_int(booking_id) else None
        if booking is None:
            return ActionResult(status="failed", message="Gagal: booking tidak ditemukan")
        if _booking_end(booking) > ctx.clock.now():
            return ActionResult(status="info", message=_short(f"Booking #{booking_id} belum selesai"))
        try:
            data.complete_booking(ctx.conn, ctx.business_id, booking_id)
        except data.BookingStateError:
            return ActionResult(status="info", message=_short(f"Booking #{booking_id} sudah diproses"))
        who = _customer_name(ctx, booking["customer_id"])
        return ActionResult(
            status="done",
            message=_short(f"Booking #{booking_id} {who} selesai"),
            data={"booking_id": booking_id},
        )


# ------------------------------------------------------------------ billing --


class BillingRole(_JasaRole):
    """Drafts one invoice per done booking, auto-approves small ones, records payments.

    ``prices`` maps a service name (case-insensitive) to its catalogue price in
    rupiah. A done booking whose service has no price is not invoiced (the role
    cannot know the amount); the owner invoices it by hand.
    """

    name = ROLE_BILLING

    def __init__(
        self,
        agent_id: int | None = None,
        payment_feed: PaymentFeed | None = None,
        auto_approve_limit_idr: int = DEFAULT_AUTO_APPROVE_LIMIT_IDR,
        prices: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(agent_id)
        self.payment_feed = payment_feed
        self.auto_approve_limit_idr = auto_approve_limit_idr
        self.set_prices(prices)

    def set_prices(self, prices: Mapping[str, int] | None) -> None:
        source = prices if prices is not None else {s.name: s.price_idr for s in SERVICE_CATALOGUE}
        self.prices = {_price_key(name): amount for name, amount in source.items()}

    # ----------------------------------------------------------- triggers --

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None:
            return []
        return self._invoice_tasks(ctx, me) + self._approve_tasks(ctx, me) + self._payment_tasks(ctx, me)

    def _invoice_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        # Any invoice counts, also a void one: an owner who voided it does not want a new one.
        rows = ctx.conn.execute(
            "SELECT b.id, b.customer_id, b.service FROM booking b "
            "WHERE b.business_id = ? AND b.status = 'done' AND NOT EXISTS ("
            "  SELECT 1 FROM invoice i WHERE i.booking_id = b.id AND i.business_id = b.business_id) "
            "ORDER BY b.starts_at, b.id",
            (ctx.business_id,),
        ).fetchall()
        tasks = []
        for booking_id, customer_id, service in rows:
            amount = self.prices.get(_price_key(service))
            if amount is None:
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={
                        "action": ACT_INVOICE,
                        "booking_id": booking_id,
                        "customer_id": customer_id,
                        "service": service,
                        "amount_idr": amount,
                        "auto_approve_limit_idr": self.auto_approve_limit_idr,
                    },
                    cost_idr=0,
                    reason=(
                        f"Buat invoice booking #{booking_id}: "
                        f"{_customer_name(ctx, customer_id)}, {service} {rupiah(amount)}"
                    ),
                )
            )
        return tasks

    def _approve_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        """Drafts of bookings that fit the limit but are still draft (a crash, or a raised limit)."""
        tasks = []
        for invoice in data.list_invoices(ctx.conn, ctx.business_id, status="draft"):
            if invoice["booking_id"] is None or invoice["amount_idr"] > self.auto_approve_limit_idr:
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={
                        "action": ACT_APPROVE,
                        "invoice_id": invoice["id"],
                        "auto_approve_limit_idr": self.auto_approve_limit_idr,
                    },
                    cost_idr=0,
                    reason=f"Setujui invoice #{invoice['id']} {rupiah(invoice['amount_idr'])} (di bawah limit)",
                )
            )
        return tasks

    def _payment_tasks(self, ctx: BusinessContext, me: int) -> list[Task]:
        if self.payment_feed is None:
            return []
        tasks, seen = [], set()
        for payment in self.payment_feed.poll(ctx):
            invoice_id = payment.get("invoice_id") if isinstance(payment, dict) else None
            if not _is_int(invoice_id) or invoice_id in seen:
                continue
            seen.add(invoice_id)
            invoice = data.get_invoice(ctx.conn, ctx.business_id, invoice_id)
            if invoice is None or invoice["status"] != "approved":
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={"action": ACT_PAYMENT, "invoice_id": invoice_id},
                    cost_idr=0,
                    reason=(
                        f"Catat pembayaran invoice #{invoice_id}: "
                        f"{_customer_name(ctx, invoice['customer_id'])} {rupiah(invoice['amount_idr'])}"
                    ),
                )
            )
        return tasks

    # ----------------------------------------------------------- handlers --

    def _handlers(self) -> dict[str, Any]:
        return {ACT_INVOICE: self._invoice, ACT_APPROVE: self._approve, ACT_PAYMENT: self._payment}

    def _limit(self, task: Task) -> int:
        limit = task.payload.get("auto_approve_limit_idr", self.auto_approve_limit_idr)
        return limit if _is_int(limit) and limit >= 0 else self.auto_approve_limit_idr

    def _invoice(self, task: Task, ctx: BusinessContext) -> ActionResult:
        payload = task.payload
        booking_id, amount = payload.get("booking_id"), payload.get("amount_idr")
        if not _is_int(booking_id) or not _is_int(amount) or amount < 0:
            return ActionResult(status="failed", message="Gagal: data invoice tidak valid")
        booking = data.get_booking(ctx.conn, ctx.business_id, booking_id)
        if booking is None:
            return ActionResult(status="failed", message="Gagal: booking tidak ditemukan")
        if booking["status"] != "done":
            return ActionResult(status="info", message=_short(f"Booking #{booking_id} belum selesai"))
        existing = ctx.conn.execute(
            "SELECT id FROM invoice WHERE business_id = ? AND booking_id = ? LIMIT 1",
            (ctx.business_id, booking_id),
        ).fetchone()
        if existing is not None:
            return ActionResult(status="info", message=_short(f"Invoice #{existing[0]} sudah ada"))
        try:
            invoice_id = data.create_invoice(
                ctx.conn, ctx.business_id, booking["customer_id"], booking["service"], amount,
                booking_id, ctx.clock,
            )
        except data.JasaError as exc:
            return ActionResult(status="failed", message=_short(f"Gagal buat invoice: {exc}"))
        return self._decide(ctx, invoice_id, amount, self._limit(task))

    def _approve(self, task: Task, ctx: BusinessContext) -> ActionResult:
        invoice_id = task.payload.get("invoice_id")
        invoice = data.get_invoice(ctx.conn, ctx.business_id, invoice_id) if _is_int(invoice_id) else None
        if invoice is None:
            return ActionResult(status="failed", message="Gagal: invoice tidak ditemukan")
        if invoice["status"] != "draft":
            return ActionResult(status="info", message=_short(f"Invoice #{invoice_id} sudah diproses"))
        return self._decide(ctx, invoice_id, invoice["amount_idr"], self._limit(task))

    @staticmethod
    def _decide(ctx: BusinessContext, invoice_id: int, amount: int, limit: int) -> ActionResult:
        """Approve by policy at or below the limit, otherwise leave the draft for the owner."""
        if amount > limit:
            return ActionResult(
                status="pending",
                message=_short(f"Invoice #{invoice_id} {rupiah(amount)} menunggu pemilik"),
                data={"invoice_id": invoice_id, "amount_idr": amount, "limit_idr": limit},
            )
        try:
            data.approve_invoice(ctx.conn, ctx.business_id, invoice_id, "policy", ctx.clock)
        except data.JasaError as exc:
            return ActionResult(status="failed", message=_short(f"Gagal setujui invoice: {exc}"))
        return ActionResult(
            status="done",
            message=_short(f"Invoice #{invoice_id} {rupiah(amount)} disetujui"),
            data={"invoice_id": invoice_id, "amount_idr": amount, "approved_by": "policy"},
        )

    @staticmethod
    def _payment(task: Task, ctx: BusinessContext) -> ActionResult:
        invoice_id = task.payload.get("invoice_id")
        if not _is_int(invoice_id):
            return ActionResult(status="failed", message="Gagal: nomor invoice tidak valid")
        try:
            data.mark_paid(ctx.conn, ctx.business_id, invoice_id, ctx.clock)
        except data.InvoiceStateError:
            return ActionResult(status="info", message=_short(f"Invoice #{invoice_id} sudah diproses"))
        except data.UnknownInvoiceError:
            return ActionResult(status="failed", message="Gagal: invoice tidak ditemukan")
        invoice = data.get_invoice(ctx.conn, ctx.business_id, invoice_id) or {}
        amount = invoice.get("amount_idr", 0)
        return ActionResult(
            status="done",
            message=_short(f"Invoice #{invoice_id} lunas {rupiah(amount)}"),
            data={"invoice_id": invoice_id, "amount_idr": amount},
        )


# ----------------------------------------------------------------- followup --


class FollowUpRole(_JasaRole):
    """Queues thank-you messages and payment reminders in the outbox (never sends)."""

    name = ROLE_FOLLOWUP

    def __init__(
        self,
        agent_id: int | None = None,
        overdue_days: int = DEFAULT_OVERDUE_DAYS,
        max_messages_per_day: int = DEFAULT_MAX_MESSAGES_PER_DAY,
    ) -> None:
        super().__init__(agent_id)
        self.overdue_days = overdue_days
        self.max_messages_per_day = max_messages_per_day

    # ------------------------------------------------- activity-log queries --

    @staticmethod
    def _queued_today(ctx: BusinessContext, me: int) -> int:
        """Message tasks of this agent today, also rejected ones (a blocked policy must not loop)."""
        start, end = _day_bounds(ctx.clock.today())
        row = ctx.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE business_id = ? AND agent_id = ? AND action = ? "
            "AND created_at >= ? AND created_at < ?",
            (ctx.business_id, me, ACTION_CUSTOMER_MESSAGE, start, end),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _rejected_today(ctx: BusinessContext, me: int) -> bool:
        """True when the policy rejected a message of this agent today.

        The Manager logs a rejection with the policy's reason, not the task reason,
        so no invoice or booking marker can be read back. Instead the role stops for
        the rest of the day, so a blocked ``customer_message`` is not retried on
        every tick.
        """
        start, end = _day_bounds(ctx.clock.today())
        row = ctx.conn.execute(
            "SELECT 1 FROM activity WHERE business_id = ? AND agent_id = ? AND action = ? "
            "AND status = 'rejected' AND created_at >= ? AND created_at < ? LIMIT 1",
            (ctx.business_id, me, ACTION_CUSTOMER_MESSAGE, start, end),
        ).fetchone()
        return row is not None

    @staticmethod
    def _last_queued(ctx: BusinessContext, marker: str) -> str | None:
        """``created_at`` of the newest queued message whose reason carries ``marker``."""
        row = ctx.conn.execute(
            "SELECT MAX(created_at) FROM activity WHERE business_id = ? AND action = ? "
            "AND status = 'done' AND reason LIKE ?",
            (ctx.business_id, ACTION_CUSTOMER_MESSAGE, f"%{marker}%"),
        ).fetchone()
        return row[0]

    # ----------------------------------------------------------- triggers --

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None:
            return []
        budget = self.max_messages_per_day - self._queued_today(ctx, me)
        if budget <= 0 or self._rejected_today(ctx, me):
            return []
        tasks = self._reminders(ctx, me) + self._thanks(ctx, me)
        return tasks[:budget]

    @staticmethod
    def _recipient(customer: dict) -> str:
        return f"{customer['name']} ({customer['phone']})" if customer["phone"] else customer["name"]

    def _reminders(self, ctx: BusinessContext, me: int) -> list[Task]:
        cutoff = _iso(ctx.clock.now() - REMINDER_INTERVAL)
        business = _business_name(ctx)
        tasks = []
        for invoice in data.unpaid_overdue(ctx.conn, ctx.business_id, ctx.clock, days=self.overdue_days):
            marker = f"(invoice #{invoice['id']})"
            last = self._last_queued(ctx, marker)
            if last is not None and last > cutoff:
                continue
            customer = data.get_customer(ctx.conn, ctx.business_id, invoice["customer_id"])
            if customer is None:
                continue
            amount = invoice["amount_idr"]
            body = (
                f"Halo Kak {customer['name']}, kami mengingatkan tagihan {invoice['description']} "
                f"sebesar {rupiah(amount)} (invoice #{invoice['id']}) yang belum dibayar. "
                f"Mohon konfirmasi jika sudah transfer. Terima kasih. - {business}"
            )
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.CUSTOMER_MESSAGE,
                    payload={
                        "action": ACT_REMINDER,
                        "channel": MESSAGE_CHANNEL,
                        "recipient": self._recipient(customer),
                        "body": body,  # a template: the Manager keeps it, so the amount is exact
                        "invoice_id": invoice["id"],
                        "amount_idr": amount,
                    },
                    cost_idr=0,
                    reason=f"Pengingat bayar {customer['name']} {rupiah(amount)} {marker}",
                )
            )
        return tasks

    def _thanks(self, ctx: BusinessContext, me: int) -> list[Task]:
        today = date.fromisoformat(ctx.clock.today())
        business = _business_name(ctx)
        candidates: list[dict] = []
        for age in range(THANKS_MAX_AGE_DAYS, 0, -1):  # oldest first
            candidates += data.list_bookings(
                ctx.conn, ctx.business_id, day=today - timedelta(days=age), status="done"
            )
        tasks = []
        for booking in candidates:
            marker = f"(booking #{booking['id']})"
            if self._last_queued(ctx, marker) is not None:
                continue
            customer = data.get_customer(ctx.conn, ctx.business_id, booking["customer_id"])
            if customer is None:
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.CUSTOMER_MESSAGE,
                    payload={
                        # No "body": the Manager drafts the text with the strong model.
                        "action": ACT_THANKS,
                        "channel": MESSAGE_CHANNEL,
                        "recipient": self._recipient(customer),
                        "booking_id": booking["id"],
                        "customer": customer["name"],
                        "service": booking["service"],
                        "business": business,
                        "instruksi": (
                            "Tulis ucapan terima kasih WhatsApp yang ramah, maksimal dua kalimat, "
                            "untuk pelanggan setelah layanan selesai."
                        ),
                    },
                    cost_idr=0,
                    reason=f"Ucapan terima kasih {customer['name']}, {booking['service']} {marker}",
                )
            )
        return tasks

    # ----------------------------------------------------------- handlers --

    def _handlers(self) -> dict[str, Any]:
        return {ACT_REMINDER: self._reminder, ACT_THANKS: self._thank_you}

    @staticmethod
    def _reminder(task: Task, ctx: BusinessContext) -> ActionResult:
        # The Manager wrote the outbox row before this runs; nothing is sent (Req 5.4).
        who = task.payload.get("recipient", "pelanggan")
        return ActionResult(
            status="done",
            message=_short(f"Pengingat {who} masuk outbox"),
            data={"invoice_id": task.payload.get("invoice_id"), "source": "template"},
        )

    def _thank_you(self, task: Task, ctx: BusinessContext) -> ActionResult:
        payload = task.payload
        template = (
            f"Halo Kak {payload.get('customer', '')}, terima kasih sudah memakai layanan "
            f"{payload.get('service', '')} di {payload.get('business', 'kami')}. "
            "Semoga puas, kami tunggu kunjungan berikutnya!"
        )
        source = "llm"
        llm_text = payload.get("llm_text")
        if task.rule_only or not isinstance(llm_text, str) or not llm_text.strip():
            source = "template"
            self._use_template(ctx, task, template)
        return ActionResult(
            status="done",
            message=_short(f"Terima kasih {payload.get('customer', '')} masuk outbox"),
            data={"booking_id": payload.get("booking_id"), "source": source},
        )

    def _use_template(self, ctx: BusinessContext, task: Task, template: str) -> None:
        """Swap the Manager's neutral default body for the role's template after a model failure.

        The Manager writes the outbox row (status ``pending``) before ``run``. When
        the model failed, that row holds the neutral default text. Only a row that
        still holds exactly that default is changed; any other body is left alone.
        """
        default = DEFAULT_BODY_TEMPLATE.format(name=(self.name or "").strip() or DEFAULT_SENDER_NAME)
        if task.payload.get("body") != default:
            return
        cur = ctx.conn.execute(
            "UPDATE outbox SET body = ? WHERE id = (SELECT MAX(id) FROM outbox WHERE business_id = ? "
            "AND agent_id = ? AND recipient = ? AND body = ? AND status = 'pending')",
            (template, ctx.business_id, task.agent_id, task.payload.get("recipient", ""), default),
        )
        if cur.rowcount == 1:
            task.payload["body"] = template


# ------------------------------------------------------------ registry/build --

JASA_ROLES = RoleRegistry()
for _role_cls in (BookingRole, BillingRole, FollowUpRole):
    JASA_ROLES.register(_role_cls)


def build_roles(
    conn: sqlite3.Connection,
    business_id: int,
    clock: Clock,
    *,
    booking_feed: BookingFeed | None = None,
    payment_feed: PaymentFeed | None = None,
    auto_approve_limit_idr: int = DEFAULT_AUTO_APPROVE_LIMIT_IDR,
    max_messages_per_day: int = DEFAULT_MAX_MESSAGES_PER_DAY,
    overdue_days: int = DEFAULT_OVERDUE_DAYS,
    catalogue: Sequence[Service] | None = None,
) -> dict[int, Role]:
    """Map each agent row of the business to a role instance, by its ``role`` column.

    Agents whose role is not a Jasa role are skipped. ``booking_feed`` goes to the
    booking role and ``payment_feed`` to the billing role; ``None`` means no
    source, so nothing is simulated. ``catalogue`` sets the billing prices (default
    :data:`SERVICE_CATALOGUE`). ``clock`` is accepted for a uniform signature; roles
    read the clock from the context they are called with.
    """
    roles: dict[int, Role] = {}
    rows = conn.execute(
        "SELECT id, role FROM agent WHERE business_id = ? ORDER BY id", (business_id,)
    ).fetchall()
    for agent_id, role_name in ((int(r[0]), r[1]) for r in rows):
        if role_name not in JASA_ROLES:
            continue
        role = JASA_ROLES.create(role_name)
        role.agent_id = agent_id
        if isinstance(role, BookingRole):
            role.feed = booking_feed
        elif isinstance(role, BillingRole):
            role.payment_feed = payment_feed
            role.auto_approve_limit_idr = auto_approve_limit_idr
            if catalogue is not None:
                role.set_prices({s.name: s.price_idr for s in catalogue})
        elif isinstance(role, FollowUpRole):
            role.max_messages_per_day = max_messages_per_day
            role.overdue_days = overdue_days
        roles[agent_id] = role
    return roles
