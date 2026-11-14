"""Skeleton demo: the real backend and the 3D office with four DUMMY agents.

    .venv/bin/python -m skeleton.office.demo [--port 8000] [--db :memory:|path] [--interval 6]

then open http://127.0.0.1:8000/ . This is the checkpoint demo of the skeleton
(task 9). It uses everything real: SQLite, the policy engine, the Manager, the
heartbeat, the visual tracker, the activity log, the SSE stream and the front
end. Only the roles below are fake.

THE ROLES IN THIS FILE ARE DUMMIES. They contain no business logic. They only
walk through a fixed script of made-up tasks so that every visual state occurs
within a minute or two: working, handoff, waiting for the owner, idle, a spend
that is allowed, a spend that the daily limit rejects, a draft, and a customer
message that lands in the outbox (nothing is ever sent). The real roles live in
``apps/``.

* No API key and no network: the LLM is ``FakeLLMClient``. The demo ignores
  ``KANTOR_LLM_API_KEY`` on purpose, so the API never holds a secret.
* The server binds to 127.0.0.1 only.
* ``--db :memory:`` (default) starts clean every time. With a file, the demo
  business is created once and reused on the next start.

``build_demo`` returns the ``OfficeContext`` for ``create_app``. The integration
test uses it with ``autostart=False`` and drives the heartbeat by hand.

This module must not import anything from ``apps/``.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from skeleton.core import db
from skeleton.core.clock import Clock, SystemClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import install_append_only_triggers
from skeleton.office.api import create_app
from skeleton.office.context import OfficeContext
from skeleton.office.visual import VisualTracker
from skeleton.policy.engine import set_policy
from skeleton.runtime.heartbeat import HANDOFF_KEY, Heartbeat
from skeleton.runtime.llm import FakeLLMClient
from skeleton.runtime.manager import Manager
from skeleton.runtime.models import ActionResult, BaseRole, BusinessContext, Role, Task, TaskKind

HOST = "127.0.0.1"  # the demo is never reachable from another machine
DEFAULT_PORT = 8000
DEFAULT_INTERVAL_SECONDS = 6.0
BUSINESS_NAME = "Kantor Demo"
BUSINESS_TYPE = "demo"
DAILY_LIMIT_IDR = 100_000

PAYLOAD_WORK = "work_seconds"
PAYLOAD_END = "end_status"
WAITING_BUBBLE = "Menunggu OK pemilik"

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Step:
    """One made-up task of a dummy role."""

    kind: str
    reason: str
    work_seconds: float = 0.0  # how long ``run`` takes, so the working state can be seen
    cost_idr: int = 0
    handoff_to: str | None = None  # name of the colleague who does the task
    end_status: str = "done"  # "pending" makes the agent wait for the owner
    channel: str = ""
    recipient: str = ""


# Each plan is one lap. A role walks through its plan, one entry per heartbeat tick,
# and starts again from the top. ``None`` means "nothing to do this tick" (idle).
# Short tasks first, so the first minute already shows work, handoff and waiting.
_STAFF_PLAN: tuple[Step | None, ...] = (
    Step(TaskKind.READ, "Memeriksa catatan penjualan hari ini", work_seconds=5.0),
    None,
    Step(TaskKind.DRAFT, "Menyusun ide promo akhir pekan", work_seconds=5.0),
    None,
    Step(
        TaskKind.CUSTOMER_MESSAGE,
        "Mengingatkan pelanggan soal tagihan yang jatuh tempo",
        work_seconds=3.0,
        channel="whatsapp",
        recipient="Pelanggan Demo",
    ),
    None,
)
_COORDINATOR_PLAN: tuple[Step | None, ...] = (
    # Under the 8 s a handoff stays visible, long enough to walk over and talk.
    Step(TaskKind.READ, "Stok beras menipis, minta Wati menyiapkan pemesanan", work_seconds=6.5, handoff_to="Wati"),
    None,
    Step(TaskKind.READ, "Merekap stok gudang", work_seconds=5.0),
    None,
)
_ADMIN_PLAN: tuple[Step | None, ...] = (
    Step(TaskKind.READ, "Menyiapkan penawaran pemasok", work_seconds=3.0, end_status="pending"),
    None,  # three quiet ticks: the amber marker stays up while the owner "has not answered"
    None,
    None,
    Step(TaskKind.READ, "Memeriksa penawaran yang sudah disetujui", work_seconds=3.0),
    None,
)
# Amounts add up past Rp 100.000 within one lap, so the limit rejects some of them.
_FINANCE_PLAN: tuple[Step | None, ...] = (
    Step(TaskKind.SPEND, "Membeli alat tulis kantor", work_seconds=2.0, cost_idr=5_000),
    None,
    Step(TaskKind.SPEND, "Membeli kardus pengemas", work_seconds=2.0, cost_idr=20_000),
    Step(TaskKind.SPEND, "Membeli stok plastik", work_seconds=2.0, cost_idr=30_000),
    None,
    Step(TaskKind.SPEND, "Membeli bahan promo", work_seconds=2.0, cost_idr=55_000),
)

# (agent name, role label shown in the panel, plan). The desk index is the position.
DEMO_STAFF: tuple[tuple[str, str, tuple[Step | None, ...]], ...] = (
    ("Sari", "Staf Demo", _STAFF_PLAN),
    ("Budi", "Koordinator Demo", _COORDINATOR_PLAN),
    ("Wati", "Admin Demo", _ADMIN_PLAN),
    ("Joko", "Keuangan Demo", _FINANCE_PLAN),
)


class DemoRole(BaseRole):
    """DUMMY role for the skeleton demo. No business logic: it plays back a plan."""

    def __init__(
        self,
        name: str,
        agent_id: int,
        plan: tuple[Step | None, ...],
        agent_ids: dict[str, int],
        work_scale: float = 1.0,
        sleep: SleepFn | None = None,
    ) -> None:
        self.name = name
        self._agent_id = agent_id
        self._plan = plan
        self._agent_ids = agent_ids  # agent name -> id, to find a handoff target
        self._work_scale = work_scale
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._tick = 0

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        step = self._plan[self._tick % len(self._plan)]
        self._tick += 1
        if step is None:
            return []
        payload: dict = {PAYLOAD_WORK: step.work_seconds, PAYLOAD_END: step.end_status}
        if step.handoff_to is not None:
            payload[HANDOFF_KEY] = self._agent_ids[step.handoff_to]
        if step.kind == TaskKind.CUSTOMER_MESSAGE:
            # No body: the Manager asks the (fake) model for the text.
            payload.update(channel=step.channel, recipient=step.recipient)
        return [
            Task(
                agent_id=self._agent_id,
                kind=step.kind,
                payload=payload,
                cost_idr=step.cost_idr,
                reason=step.reason,
            )
        ]

    async def run(self, task: Task, ctx: BusinessContext) -> ActionResult:
        seconds = float(task.payload.get(PAYLOAD_WORK, 0.0)) * self._work_scale
        if seconds > 0:
            await self._sleep(seconds)  # pretend to work, so "working" can be seen
        if task.payload.get(PAYLOAD_END) == "pending":
            return ActionResult(status="pending", message=WAITING_BUBBLE)
        text = task.payload.get("llm_text")
        return ActionResult(status="done", message=str(text)[:200] if text else task.reason)


# ---------------------------------------------------------------------- #
# Building the demo
# ---------------------------------------------------------------------- #
def seed_demo(conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """Create the demo business, its policy and 4 agents unless they exist.

    Returns ``(business_id, {agent name: agent id})``. An existing file database
    keeps its policy, pause flags and history.
    """
    row = conn.execute(
        "SELECT id FROM business WHERE name = ? AND type = ? ORDER BY id LIMIT 1",
        (BUSINESS_NAME, BUSINESS_TYPE),
    ).fetchone()
    if row is not None:
        business_id = int(row["id"])
    else:
        business_id = int(
            conn.execute(
                "INSERT INTO business (name, type) VALUES (?, ?)", (BUSINESS_NAME, BUSINESS_TYPE)
            ).lastrowid
        )
    if conn.execute("SELECT 1 FROM policy WHERE business_id = ?", (business_id,)).fetchone() is None:
        set_policy(conn, business_id, DAILY_LIMIT_IDR, [])  # nothing blocked

    ids: dict[str, int] = {}
    for desk_index, (name, role_label, _plan) in enumerate(DEMO_STAFF):
        found = conn.execute(
            "SELECT id FROM agent WHERE business_id = ? AND desk_index = ?", (business_id, desk_index)
        ).fetchone()
        if found is not None:
            ids[name] = int(found["id"])
            continue
        ids[name] = int(
            conn.execute(
                "INSERT INTO agent (business_id, role, name, desk_index) VALUES (?, ?, ?, ?)",
                (business_id, role_label, name, desk_index),
            ).lastrowid
        )
    return business_id, ids


def build_demo(
    db_path: str = ":memory:",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    autostart: bool = True,
    work_scale: float = 1.0,
    sleep: SleepFn | None = None,
    clock: Clock | None = None,
) -> OfficeContext:
    """Wire the real skeleton around the dummy roles and return the office context.

    ``work_scale`` and ``sleep`` only change how long the dummy roles "work"
    (tests use them). Nothing here reads the environment, so there is no API key.
    """
    conn = db.connect(db_path, check_same_thread=False)
    db.init_schema(conn)
    install_append_only_triggers(conn)
    business_id, ids = seed_demo(conn)

    settings = Settings(
        heartbeat_seconds=interval_seconds,  # type: ignore[arg-type]  # a float >= 1 works
        model_low="demo-low",
        model_strong="demo-strong",
        db_path=db_path,
    )
    clock = clock if clock is not None else SystemClock()
    bus = EventBus()
    roles: dict[int, Role] = {
        ids[name]: DemoRole(name, ids[name], plan, ids, work_scale=work_scale, sleep=sleep)
        for name, _label, plan in DEMO_STAFF
    }
    manager = Manager(conn, business_id, clock, FakeLLMClient(), settings, roles, bus)
    heartbeat = Heartbeat(conn, business_id, clock, manager, roles, settings, bus)
    tracker = VisualTracker(conn, business_id, bus, clock, heartbeat)
    return OfficeContext(
        conn=conn,
        business_id=business_id,
        clock=clock,
        bus=bus,
        settings=settings,
        heartbeat=heartbeat,
        tracker=tracker,
        manager=manager,
        autostart=autostart,
    )


# ---------------------------------------------------------------------- #
# Command line
# ---------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m skeleton.office.demo",
        description="Skeleton demo: the real backend and the 3D office with four dummy agents.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (default 8000)")
    parser.add_argument("--db", default=":memory:", help=":memory: (default) or a SQLite file path")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between heartbeat ticks, at least 1 (default 6)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    import uvicorn  # imported here so tests can import this module without starting a server

    app = create_app(build_demo(args.db, args.interval))
    print(f"Kantor Agen skeleton demo: http://{HOST}:{args.port}/  (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
