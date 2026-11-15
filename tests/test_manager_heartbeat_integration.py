"""Manager + Heartbeat + policy engine + activity log, wired together.

Covers Req 4.1, 4.2, 4.4, 5.4 and 7.1 end to end. Fake LLM and fake clock only:
no network and no real waiting. Single-component behaviour is already covered
in test_manager.py and test_heartbeat.py, so these tests always run a full tick.
"""
import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.log.activity import ALLOWED_STATUSES, recent
from skeleton.policy import set_policy, spent_today
from skeleton.runtime import (
    ActionResult,
    BaseRole,
    FakeLLMClient,
    Heartbeat,
    Manager,
    Task,
    TaskKind,
)

API_KEY = "sk-secret-123456"
LOW = "model-low"
STRONG = "model-strong"
LLM_DOWN = "Model AI tidak tersedia, lanjut tanpa model"  # Indonesian since task 14.1
DAILY_LIMIT = 10_000
DEFAULT_BODY = "Pesan dari {name}: lihat detail di kantor."
HEARTBEAT_SECONDS = 300


def task(agent_id, kind, reason, cost=0, payload=None):
    return Task(
        agent_id=agent_id,
        kind=kind,
        payload=payload if payload is not None else {},
        cost_idr=cost,
        reason=reason,
    )


class ScriptedRole(BaseRole):
    """Emits the same tasks on every tick and records what ``run`` saw."""

    def __init__(self, name, make_tasks=lambda: [], run_error=None, trigger_error=None):
        self.name = name
        self._make_tasks = make_tasks
        self._run_error = run_error
        self._trigger_error = trigger_error
        self.runs: list[Task] = []
        self.seen_text: list = []
        self.seen_rule_only: list[bool] = []

    def triggers(self, ctx):
        if self._trigger_error is not None:
            raise self._trigger_error
        return self._make_tasks()

    def run(self, task, ctx):
        self.runs.append(task)
        self.seen_text.append(task.payload.get("llm_text"))
        self.seen_rule_only.append(task.rule_only)
        if self._run_error is not None:
            raise self._run_error
        return ActionResult(status="done", message=f"{self.name} ok")


class RoutingManager(Manager):
    """Test wiring: a task with ``handoff_to`` in its payload goes through ``assign``.

    The heartbeat only calls ``handle``; this is the smallest way to make it
    exercise ``assign`` (Req 4.1) inside a real tick.
    """

    async def handle(self, task):
        target = task.payload.get("handoff_to")
        if target is None:
            return await super().handle(task)
        return await self.assign(task, target)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id in (1, 2, 3):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', ?, ?)",
            (agent_id, f"Agen {agent_id}", agent_id),
        )
    set_policy(c, 1, DAILY_LIMIT, [])
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def llm():
    return FakeLLMClient()


def wire(conn, clock, llm, roles, manager_cls=Manager):
    settings = Settings(
        llm_api_key=API_KEY, model_low=LOW, model_strong=STRONG, heartbeat_seconds=HEARTBEAT_SECONDS
    )
    manager = manager_cls(conn, 1, clock, llm, settings, roles)
    return Heartbeat(conn, 1, clock, manager, roles, settings)


def log(conn):
    """Activity rows, oldest first."""
    return list(reversed(recent(conn, 1, limit=500)))


def outbox(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def ledger_total(conn):
    return conn.execute("SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger").fetchone()[0]


# --------------------------------------------------------------------------- #
# Model routing seen end to end (Req 4.3 through a real tick)
# --------------------------------------------------------------------------- #
async def test_tick_routes_each_kind_to_its_model_in_task_order(conn, clock, llm):
    llm.responses.extend(["ringkasan singkat", "Halo Kak, pesanan siap diambil."])
    roles = {
        1: ScriptedRole(
            "gudang",
            lambda: [
                task(1, TaskKind.READ, "cek stok"),
                task(1, TaskKind.DRAFT, "ringkas stok", payload={"topik": "stok"}),
                task(1, TaskKind.SPEND, "beli telur", cost=2_000),
            ],
        ),
        2: ScriptedRole(
            "kasir",
            lambda: [
                task(2, TaskKind.CUSTOMER_MESSAGE, "kabari pelanggan", payload={"recipient": "0812"})
            ],
        ),
    }
    results = await wire(conn, clock, llm, roles).tick()

    assert [r.status for r in results] == ["done"] * 4
    # Only draft (low) and customer_message (strong) reach the model, in task order.
    assert [c["model"] for c in llm.calls] == [LOW, STRONG]
    assert roles[1].seen_text == [None, "ringkasan singkat", None]
    assert roles[2].seen_text == ["Halo Kak, pesanan siap diambil."]
    # The reply drafted for the customer is what sits in the outbox (Req 5.4).
    [message] = outbox(conn)
    assert message["body"] == "Halo Kak, pesanan siap diambil."
    assert message["status"] == "pending"
    assert message["agent_id"] == 2
    assert [r["action"] for r in log(conn)] == [
        "Periksa data", "Tulis draf", "Belanja", "Kirim pesan pelanggan",  # default labels of the four kinds
    ]


async def test_routine_tick_uses_no_model_calls(conn, clock, llm):
    roles = {
        1: ScriptedRole("gudang", lambda: [task(1, TaskKind.READ, "cek stok")]),
        2: ScriptedRole("kas", lambda: [task(2, TaskKind.SPEND, "bayar listrik", cost=1_000)]),
    }
    await wire(conn, clock, llm, roles).tick()
    assert llm.calls == []


# --------------------------------------------------------------------------- #
# LLM failure during a tick (Req 4.4)
# --------------------------------------------------------------------------- #
async def test_llm_failure_mid_tick_continues_rule_only_and_logs_failed(conn, clock, llm):
    llm.fail_next(2)  # the draft and the customer message both lose the model
    roles = {
        1: ScriptedRole("gudang", lambda: [task(1, TaskKind.DRAFT, "ringkas stok")]),
        2: ScriptedRole("kasir", lambda: [task(2, TaskKind.CUSTOMER_MESSAGE, "kabari pelanggan")]),
        3: ScriptedRole("kas", lambda: [task(3, TaskKind.SPEND, "beli gas", cost=3_000)]),
    }
    results = await wire(conn, clock, llm, roles).tick()

    # Every task still ran, and the spend after the failures was not affected.
    assert [r.status for r in results] == ["done", "done", "done"]
    assert roles[1].seen_text == [None] and roles[1].seen_rule_only == [True]
    assert roles[2].seen_text == [None] and roles[2].seen_rule_only == [True]
    assert roles[3].seen_rule_only == [False]
    assert ledger_total(conn) == 3_000
    # No body from the role and no model: the outbox gets the neutral default.
    [message] = outbox(conn)
    assert message["body"] == DEFAULT_BODY.format(name="kasir")

    failed = [r for r in log(conn) if r["status"] == "failed"]
    assert [(r["agent_id"], r["action"]) for r in failed] == [(1, "Model AI"), (2, "Model AI")]
    assert all(r["reason"] == LLM_DOWN for r in failed)
    assert all(API_KEY not in r["reason"] and "fake LLM failure" not in r["reason"] for r in failed)


async def test_model_is_used_again_on_the_next_tick_after_a_failure(conn, clock, llm):
    llm.fail_next()
    roles = {1: ScriptedRole("gudang", lambda: [task(1, TaskKind.DRAFT, "ringkas stok")])}
    hb = wire(conn, clock, llm, roles)

    await hb.tick()
    clock.advance(HEARTBEAT_SECONDS)
    await hb.tick()

    assert roles[1].seen_text[0] is None and roles[1].seen_rule_only[0] is True
    assert roles[1].seen_text[1] is not None and roles[1].seen_rule_only[1] is False
    assert len([r for r in log(conn) if r["status"] == "failed"]) == 1


async def test_llm_failure_keeps_the_role_template_body_in_the_outbox(conn, clock, llm):
    llm.fail_next()
    roles = {
        1: ScriptedRole(
            "kasir",
            lambda: [
                task(1, TaskKind.CUSTOMER_MESSAGE, "tagih", payload={"body": "Halo, tagihan jatuh tempo."})
            ],
        )
    }
    await wire(conn, clock, llm, roles).tick()
    assert [m["body"] for m in outbox(conn)] == ["Halo, tagihan jatuh tempo."]


# --------------------------------------------------------------------------- #
# Handoff order across two agents in one tick (Req 4.1)
# --------------------------------------------------------------------------- #
async def test_handoffs_in_one_tick_keep_task_order_and_attribution(conn, clock, llm):
    roles = {
        1: ScriptedRole(
            "gudang",
            lambda: [
                task(1, TaskKind.SPEND, "beli beras", cost=4_000, payload={"handoff_to": 2}),
                task(1, TaskKind.READ, "cek stok"),
            ],
        ),
        2: ScriptedRole(
            "kas",
            lambda: [task(2, TaskKind.READ, "cek kas", payload={"handoff_to": 1})],
        ),
    }
    results = await wire(conn, clock, llm, roles, RoutingManager).tick()

    assert [r.status for r in results] == ["done", "done", "done"]
    # Tasks run in agent order, then task order. A handoff row comes right before
    # the work it moves, and the work is logged under the agent that did it.
    assert [(r["agent_id"], r["action"]) for r in log(conn)] == [
        (1, "Serah tugas"),
        (2, "Belanja"),
        (1, "Periksa data"),
        (2, "Serah tugas"),
        (1, "Periksa data"),
    ]
    handoffs = [r for r in log(conn) if r["action"] == "Serah tugas"]
    # The agents of this test have no name rows that differ, so the reason names them (or "staf #id").
    assert "menyerahkan tugas ke" in handoffs[0]["reason"]
    assert "beli beras" in handoffs[0]["reason"]
    assert "menyerahkan tugas ke" in handoffs[1]["reason"]
    # The receiving agent ran the work, and the spend is booked to it.
    assert [t.reason for t in roles[2].runs] == ["beli beras"]
    assert [t.reason for t in roles[1].runs] == ["cek stok", "cek kas"]
    assert [r["agent_id"] for r in conn.execute("SELECT agent_id FROM spend_ledger")] == [2]


async def test_handoff_to_unknown_agent_fails_and_the_tick_goes_on(conn, clock, llm):
    roles = {
        1: ScriptedRole(
            "gudang",
            lambda: [
                task(1, TaskKind.READ, "cek stok", payload={"handoff_to": 99}),
                task(1, TaskKind.READ, "cek rak"),
            ],
        )
    }
    results = await wire(conn, clock, llm, roles, RoutingManager).tick()

    assert [r.status for r in results] == ["failed", "done"]
    assert [t.reason for t in roles[1].runs] == ["cek rak"]
    rows = log(conn)
    assert [(r["action"], r["status"]) for r in rows] == [("Serah tugas", "failed"), ("Periksa data", "done")]


# --------------------------------------------------------------------------- #
# Competing spends in one tick (Req 4.2, 5.1)
# --------------------------------------------------------------------------- #
async def test_second_competing_spend_is_rejected_and_ledger_stays_within_limit(conn, clock, llm):
    roles = {
        1: ScriptedRole("gudang", lambda: [task(1, TaskKind.SPEND, "restock", cost=6_000)]),
        2: ScriptedRole("promo", lambda: [task(2, TaskKind.SPEND, "iklan", cost=6_000)]),
        3: ScriptedRole("kas", lambda: [task(3, TaskKind.SPEND, "bayar listrik", cost=4_000)]),
    }
    results = await wire(conn, clock, llm, roles).tick()

    # The earlier agent wins, the conflicting one is rejected, a smaller one still fits.
    assert [r.status for r in results] == ["done", "rejected", "done"]
    assert results[1].message == "daily spending limit reached"
    assert roles[2].runs == []
    assert ledger_total(conn) == 10_000 == spent_today(conn, 1, clock) <= DAILY_LIMIT
    assert [r["agent_id"] for r in conn.execute("SELECT agent_id FROM spend_ledger ORDER BY id")] == [1, 3]
    rows = log(conn)
    assert [(r["agent_id"], r["status"], r["cost_idr"]) for r in rows] == [
        (1, "done", 6_000),
        (2, "rejected", 0),
        (3, "done", 4_000),
    ]


# --------------------------------------------------------------------------- #
# Containment across a whole tick
# --------------------------------------------------------------------------- #
async def test_role_exceptions_are_contained_for_the_whole_tick(conn, clock, llm):
    roles = {
        1: ScriptedRole(
            "rusak-run",
            lambda: [task(1, TaskKind.READ, "cek stok")],
            run_error=RuntimeError(f"exploded with {API_KEY}"),
        ),
        2: ScriptedRole(
            "rusak-trigger", trigger_error=ValueError(f"bad trigger {API_KEY}")
        ),
        3: ScriptedRole("kas", lambda: [task(3, TaskKind.SPEND, "bayar listrik", cost=2_000)]),
    }
    hb = wire(conn, clock, llm, roles)
    results = await hb.tick()

    assert [r.status for r in results] == ["failed", "done"]
    assert hb.running == {}
    assert ledger_total(conn) == 2_000
    failed = [r for r in log(conn) if r["status"] == "failed"]
    assert sorted(r["agent_id"] for r in failed) == [1, 2]
    assert all(API_KEY not in r["reason"] and r["cost_idr"] == 0 for r in failed)
    assert any("RuntimeError" in r["reason"] for r in failed)
    assert any("ValueError" in r["reason"] for r in failed)
    # The next tick still runs normally.
    clock.advance(HEARTBEAT_SECONDS)
    assert len(await hb.tick()) == 2


async def test_failing_role_that_was_spend_authorized_costs_nothing_in_the_log(conn, clock, llm):
    roles = {
        1: ScriptedRole(
            "kas",
            lambda: [task(1, TaskKind.SPEND, "bayar listrik", cost=2_000)],
            run_error=RuntimeError("boom"),
        )
    }
    [result] = await wire(conn, clock, llm, roles).tick()
    assert result.status == "failed"
    [row] = log(conn)
    assert (row["status"], row["cost_idr"]) == ("failed", 0)


# --------------------------------------------------------------------------- #
# Every handled task is in the activity log with the full record (Req 7.1)
# --------------------------------------------------------------------------- #
async def test_every_handled_task_has_a_complete_activity_row(conn, clock, llm):
    set_policy(conn, 1, 5_000, ["customer_message"])
    roles = {
        1: ScriptedRole(
            "gudang",
            lambda: [
                task(1, TaskKind.READ, "cek stok"),
                task(1, TaskKind.DRAFT, "ringkas stok"),
                task(1, TaskKind.SPEND, "beli beras", cost=3_000),
            ],
        ),
        2: ScriptedRole(
            "kasir",
            lambda: [
                task(2, TaskKind.SPEND, "beli telur", cost=3_000),  # over the limit
                task(2, TaskKind.CUSTOMER_MESSAGE, "kabari pelanggan"),  # blocked
            ],
        ),
        3: ScriptedRole("rusak", lambda: [task(3, TaskKind.READ, "cek rak")], run_error=RuntimeError("x")),
    }
    hb = wire(conn, clock, llm, roles)
    results = await hb.tick()
    rows = log(conn)

    # The customer_message was drafted first, so the model was called once for it
    # even though the policy then blocked it. No LLM failure, so one row per task.
    assert len(rows) == len(results) == 6
    expected = [
        (1, "Periksa data", "done", 0, "cek stok"),
        (1, "Tulis draf", "done", 0, "ringkas stok"),
        (1, "Belanja", "done", 3_000, "beli beras"),
        (2, "Belanja", "rejected", 0, "Belanja Rp 3.000 ditolak: batas harian Rp 5.000 tercapai"),
        (2, "Kirim pesan pelanggan", "rejected", 0, "ditolak: jenis aksi ini diblokir pemilik"),
        (3, "Periksa data", "failed", 0, "role failed"),
    ]
    for row, (agent_id, action, status, cost, reason_part) in zip(rows, expected):
        assert row["agent_id"] == agent_id
        assert row["action"] == action
        assert row["status"] == status
        assert row["cost_idr"] == cost
        assert reason_part in row["reason"]
    for row, result in zip(rows, results):
        assert row["status"] == result.status
        assert row["cost_idr"] == result.cost_idr
    # The record fields are all present and well formed.
    for row in rows:
        assert isinstance(row["agent_id"], int)
        assert row["action"].strip() and row["reason"].strip()
        assert isinstance(row["cost_idr"], int) and row["cost_idr"] >= 0
        assert row["status"] in ALLOWED_STATUSES
        assert row["created_at"]
    assert outbox(conn) == []  # the blocked message was not queued (Req 5.4)


async def test_queued_customer_message_row_names_the_outbox_and_costs_nothing(conn, clock, llm):
    roles = {
        1: ScriptedRole(
            "kasir",
            lambda: [task(1, TaskKind.CUSTOMER_MESSAGE, "kabari pelanggan", payload={"body": "Halo"})],
        )
    }
    await wire(conn, clock, llm, roles).tick()
    [row] = log(conn)
    assert (row["agent_id"], row["action"], row["status"], row["cost_idr"]) == (
        1,
        "Kirim pesan pelanggan",
        "done",
        0,
    )
    assert "kabari pelanggan" in row["reason"] and "outbox" in row["reason"]


# --------------------------------------------------------------------------- #
# Full day: three ticks, spending capped per day
# --------------------------------------------------------------------------- #
def day_roles():
    return {
        1: ScriptedRole("gudang", lambda: [task(1, TaskKind.SPEND, "restock", cost=4_000)]),
        2: ScriptedRole("promo", lambda: [task(2, TaskKind.SPEND, "iklan", cost=3_000)]),
    }


async def test_three_ticks_in_a_day_cap_spending_at_the_daily_limit(conn, clock, llm):
    hb = wire(conn, clock, llm, day_roles())
    tick_results = []
    spent_after_tick = []
    for _ in range(3):
        tick_results.append([r.status for r in await hb.tick()])
        spent_after_tick.append(spent_today(conn, 1, clock))
        clock.advance(HEARTBEAT_SECONDS)

    assert tick_results == [
        ["done", "done"],  # 4000 + 3000 = 7000
        ["rejected", "done"],  # 4000 would reach 11000, 3000 still fits: 10000
        ["rejected", "rejected"],  # the limit is used up
    ]
    assert spent_after_tick == [7_000, 10_000, 10_000]
    assert all(spent <= DAILY_LIMIT for spent in spent_after_tick)
    # What the log says was spent matches the ledger exactly.
    logged = sum(r["cost_idr"] for r in log(conn) if r["action"] == "Belanja" and r["status"] == "done")
    assert logged == ledger_total(conn) == 10_000
    assert len([r for r in log(conn) if r["status"] == "rejected"]) == 3
    assert llm.calls == []


async def test_spending_budget_resets_on_the_next_day(conn, clock, llm):
    hb = wire(conn, clock, llm, day_roles())
    for _ in range(3):
        await hb.tick()
        clock.advance(HEARTBEAT_SECONDS)
    assert spent_today(conn, 1, clock) == 10_000
    day_one = clock.today()

    clock.advance(24 * 60 * 60)
    assert clock.today() != day_one
    assert spent_today(conn, 1, clock) == 0
    assert [r.status for r in await hb.tick()] == ["done", "done"]
    assert spent_today(conn, 1, clock) == 7_000
    assert ledger_total(conn) == 17_000
