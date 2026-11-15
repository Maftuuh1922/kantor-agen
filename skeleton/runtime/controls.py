"""Owner pause controls (Req 8.1).

One pause flag for the whole office (``business.paused``) and one per agent
(``agent.paused``). The heartbeat reads these flags before every task, so a
change takes effect on the next task step (Req 8.3).

Both helpers take an optional ``bus``. When given, a successful change publishes
``{"type": "pause_changed"}`` so the visual tracker can update at once. Without
a bus nothing is published (the old behavior).

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import sqlite3

from skeleton.core.events import EventBus

EVENT_PAUSE_CHANGED = "pause_changed"


def _check_flag(paused: object) -> int:
    if not isinstance(paused, bool):
        raise ValueError("paused must be a bool")
    return 1 if paused else 0


def _announce(bus: EventBus | None) -> None:
    if bus is not None:
        bus.publish({"type": EVENT_PAUSE_CHANGED})


def set_business_paused(
    conn: sqlite3.Connection, business_id: int, paused: bool, bus: EventBus | None = None
) -> bool:
    """Pause or resume the whole office. Returns False when the business is unknown."""
    flag = _check_flag(paused)
    cur = conn.execute("UPDATE business SET paused = ? WHERE id = ?", (flag, business_id))
    changed = cur.rowcount > 0
    if changed:
        _announce(bus)
    return changed


def set_agent_paused(
    conn: sqlite3.Connection, agent_id: int, paused: bool, bus: EventBus | None = None
) -> bool:
    """Pause or resume one agent. Returns False when the agent is unknown."""
    flag = _check_flag(paused)
    cur = conn.execute("UPDATE agent SET paused = ? WHERE id = ?", (flag, agent_id))
    changed = cur.rowcount > 0
    if changed:
        _announce(bus)
    return changed
