"""The Kantor Warung business type for the skeleton's onboarding (Req 1.2, 10.1).

Four AI staff, in desk order: Sari (cashier), Budi (stock), Wati (finance) and
Joko (marketing). The first names are chosen so the 3D office gives Sari and Wati
women's looks and Budi and Joko men's looks (``char-look.js``). ``agent.role``
holds the role key (``cashier``, ``stock``, ...), which ``build_roles`` maps to the
role classes; the Indonesian label is what the owner reads.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""
from __future__ import annotations

import sqlite3

from apps.kantor_warung.roles import (
    ROLE_CASHIER,
    ROLE_FINANCE,
    ROLE_MARKETING,
    ROLE_STOCK,
    SimulatedSalesFeed,
    build_roles,
)
from apps.kantor_warung.seed import seed_warung
from skeleton.core.clock import Clock
from skeleton.office.onboarding import AgentSpec, BusinessTemplate
from skeleton.runtime.models import Role

WARUNG_KEY = "warung"
SUGGESTED_DAILY_LIMIT_IDR = 100_000


def build_warung_roles(
    conn: sqlite3.Connection,
    business_id: int,
    clock: Clock,
    *,
    real_hours: bool = False,
    interval_seconds: float | None = None,
) -> dict[int, Role]:
    """Role instances for the Warung agents. The cashier gets the simulated walk-in customers.

    By default (``real_hours=False``) this is the demo: the simulated customers come at any hour and a
    delivery takes 1 minute instead of 2 hours. ``real_hours=True`` (``--real-hours``) keeps the shop
    hours and the real delivery time. ``interval_seconds`` is the heartbeat interval: a short one
    scales the simulated sales down so the stock does not run out in minutes (see ``roles.py``).
    """
    feed = SimulatedSalesFeed(demo_hours=not real_hours, interval_seconds=interval_seconds)
    return build_roles(conn, business_id, clock, feed=feed, demo=not real_hours)


WARUNG_TEMPLATE = BusinessTemplate(
    key=WARUNG_KEY,
    label="Warung / Toko",
    description="Warung, toko kelontong, atau kios. Staf AI mencatat penjualan, menjaga stok, "
    "merapikan keuangan, dan membuat promo.",
    agents=(
        AgentSpec(ROLE_CASHIER, "Sari", "Kasir"),
        AgentSpec(ROLE_STOCK, "Budi", "Stok"),
        AgentSpec(ROLE_FINANCE, "Wati", "Keuangan"),
        AgentSpec(ROLE_MARKETING, "Joko", "Marketing"),
    ),
    seed=seed_warung,
    build_roles=build_warung_roles,
    default_policy=(SUGGESTED_DAILY_LIMIT_IDR, ()),
)
