"""The Kantor Jasa business type for the skeleton's onboarding (Req 1.2, 10.1).

Three AI staff, in desk order: Dewi (booking), Agus (billing) and Rina (follow-up).
The first names are chosen so the 3D office gives Dewi and Rina women's looks and
Agus a man's look (``char-look.js``). ``agent.role`` holds the role key
(``booking``, ``billing``, ``followup``), which ``build_roles`` maps to the role
classes; the Indonesian label is what the owner reads.

``seed`` is ``seed_jasa``: it creates the Jasa tables first and then the demo data,
because the skeleton schema has no Jasa tables.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""
from __future__ import annotations

import sqlite3

from apps.kantor_jasa.roles import (
    ROLE_BILLING,
    ROLE_BOOKING,
    ROLE_FOLLOWUP,
    SimulatedBookingFeed,
    SimulatedPaymentFeed,
    build_roles,
)
from apps.kantor_jasa.seed import DEMO_PAYMENT_SEED, seed_jasa
from skeleton.core.clock import Clock
from skeleton.office.onboarding import AgentSpec, BusinessTemplate
from skeleton.runtime.models import Role

JASA_KEY = "jasa"
SUGGESTED_DAILY_LIMIT_IDR = 100_000  # the Jasa staff spend nothing; the policy still gates messages


def build_jasa_roles(
    conn: sqlite3.Connection,
    business_id: int,
    clock: Clock,
    *,
    real_hours: bool = False,
    interval_seconds: float | None = None,
) -> dict[int, Role]:
    """Role instances for the Jasa agents, with the simulated walk-in requests and payments.

    By default (``real_hours=False``) this is the demo: booking requests arrive at any hour.
    ``real_hours=True`` (``--real-hours``) keeps the working hours. ``interval_seconds`` is the heartbeat
    interval: a short one raises the arrival chance (see ``roles.py``).
    """
    return build_roles(
        conn,
        business_id,
        clock,
        booking_feed=SimulatedBookingFeed(demo_hours=not real_hours, interval_seconds=interval_seconds),
        payment_feed=SimulatedPaymentFeed(seed=DEMO_PAYMENT_SEED),
    )


JASA_TEMPLATE = BusinessTemplate(
    key=JASA_KEY,
    label="Kantor Jasa",
    description="Usaha jasa kecil seperti salon, bengkel, bimbel, atau servis AC. Staf AI mengatur "
    "jadwal booking, membuat dan menagih invoice, lalu menghubungi pelanggan lagi.",
    agents=(
        AgentSpec(ROLE_BOOKING, "Dewi", "Booking"),
        AgentSpec(ROLE_BILLING, "Agus", "Tagihan"),
        AgentSpec(ROLE_FOLLOWUP, "Rina", "Tindak lanjut"),
    ),
    seed=seed_jasa,
    build_roles=build_jasa_roles,
    default_policy=(SUGGESTED_DAILY_LIMIT_IDR, ()),
)
