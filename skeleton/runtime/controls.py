"""Owner pause controls (Req 8.1).

One pause flag for the whole office (``business.paused``) and one per agent
(``agent.paused``). The heartbeat reads these flags before every task, so a
change takes effect on the next task step (Req 8.3).

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import sqlite3


def _check_flag(paused: object) -> int:
    if not isinstance(paused, bool):
        raise ValueError("paused must be a bool")
    return 1 if paused else 0


def set_business_paused(conn: sqlite3.Connection, business_id: int, paused: bool) -> bool:
    """Pause or resume the whole office. Returns False when the business is unknown."""
    flag = _check_flag(paused)
    cur = conn.execute("UPDATE business SET paused = ? WHERE id = ?", (flag, business_id))
    return cur.rowcount > 0


def set_agent_paused(conn: sqlite3.Connection, agent_id: int, paused: bool) -> bool:
    """Pause or resume one agent. Returns False when the agent is unknown."""
    flag = _check_flag(paused)
    cur = conn.execute("UPDATE agent SET paused = ? WHERE id = ?", (flag, agent_id))
    return cur.rowcount > 0
