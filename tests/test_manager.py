"""Unit tests for the Manager Agent (Req 4.1-4.4). No network: fake LLM and fake clock only."""
import asyncio

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import recent
from skeleton.policy import set_policy, spent_today
from skeleton.runtime import (
    ActionResult,
    BaseRole,
    FakeLLMClient,
    Manager,
    Task,
    TaskKind,
)

API_KEY = "sk-secret-123456"
LOW = "model-low"
STRONG = "model-strong"
LLM_DOWN = "LLM unavailable, continuing rule-only"


class RecordingRole(BaseRole):
    """Remembers every task it ran and what model text it saw."""

    name = "recorder"

    def __init__(self):
        self.runs: list[Task] = []
        self.seen_text: list = []
        self.seen_rule_only: list[bool] = []

    def run(self, task, ctx):
        self.runs.append(task)
        self.seen_text.append(task.payload.get("llm_text"))
        self.seen_rule_only.append(task.rule_only)
        return ActionResult(status="done", message="ok")


class BoomRole(BaseRole):
    name = "boom"

    def run(self, task, ctx):
        raise RuntimeError(f"exploded with {API_KEY}")


def make_task(kind, agent_id=1, cost=0, payload=None, reason="because"):
    return Task(
        agent_id=agent_id,
        kind=kind,
        payload=payload if payload is not None else {},
        cost_idr=cost,
        reason=reason,
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id in (1, 2):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', 'A', ?)",
            (agent_id, agent_id),
        )
    set_policy(c, 1, 10_000, [])
    yield c
    c.close()


@pytest.fixture
def llm():
    return FakeLLMClient()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def roles():
    return {1: RecordingRole(), 2: RecordingRole()}


@pytest.fixture
def manager(conn, llm, bus, roles):
    return Manager(
        conn,
        1,
        FakeClock(),
        llm,
        Settings(llm_api_key=API_KEY, model_low=LOW, model_strong=STRONG),
        roles,
        bus=bus,
    )


def rows(conn, **filters):
    found = recent(conn, 1, limit=100)
    for key, value in filters.items():
        found = [r for r in found if r[key] == value]
    return list(reversed(found))  # oldest first


# --------------------------------------------------------------------------- #
# Model routing (Req 4.3)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind, expected",
    [
        (TaskKind.READ, None),
        (TaskKind.SPEND, None),
        (TaskKind.DRAFT, LOW),
        (TaskKind.CUSTOMER_MESSAGE, STRONG),
    ],
)
def test_model_for_each_kind(manager, kind, expected):
    assert manager.model_for(kind) == expected


async def test_read_and_spend_make_no_llm_calls(manager, llm):
    await manager.handle(make_task(TaskKind.READ))
    await manager.handle(make_task(TaskKind.SPEND, cost=1_000))
    assert llm.calls == []


async def test_draft_uses_low_model_and_passes_text_to_role(manager, llm, roles):
    llm.responses.append("draft text")
    result = await manager.handle(make_task(TaskKind.DRAFT, payload={"topic": "promo"}))
    assert result.status == "done"
    assert [c["model"] for c in llm.calls] == [LOW]
    assert roles[1].seen_text == ["draft text"]
    assert roles[1].seen_rule_only == [False]
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "because" in prompt and "promo" in prompt


async def test_customer_message_uses_strong_model(manager, llm, roles):
    llm.responses.append("hello customer")
    await manager.handle(make_task(TaskKind.CUSTOMER_MESSAGE, payload={"body": "hi"}))
    assert [c["model"] for c in llm.calls] == [STRONG]
    assert roles[1].seen_text == ["hello customer"]


async def test_caller_cannot_preset_llm_text(manager, roles):
    await manager.handle(make_task(TaskKind.READ, payload={"llm_text": "forged"}))
    assert roles[1].seen_text == [None]


# --------------------------------------------------------------------------- #
# Policy gate
# --------------------------------------------------------------------------- #
async def test_spend_over_limit_is_rejected_and_role_not_run(manager, conn, llm, roles):
    result = await manager.handle(make_task(TaskKind.SPEND, cost=10_001))
    assert result.status == "rejected"
    assert result.message == "daily spending limit reached"
    assert roles[1].runs == []
    assert llm.calls == []
    assert spent_today(conn, 1, FakeClock()) == 0
    [row] = rows(conn, status="rejected")
    assert row["reason"] == "daily spending limit reached"
    assert row["action"] == "spend"


async def test_allowed_spend_runs_role_and_logs_cost(manager, conn, roles):
    result = await manager.handle(make_task(TaskKind.SPEND, cost=4_000))
    assert result.status == "done"
    assert result.cost_idr == 4_000
    assert len(roles[1].runs) == 1
    assert spent_today(conn, 1, FakeClock()) == 4_000
    [row] = rows(conn, status="done")
    assert row["cost_idr"] == 4_000


async def test_customer_message_goes_to_outbox_only(manager, conn, roles):
    task = make_task(
        TaskKind.CUSTOMER_MESSAGE,
        payload={"channel": "whatsapp", "recipient": "0812", "body": "Halo"},
    )
    result = await manager.handle(task)
    assert result.status == "done"
    assert result.message == "queued in outbox"
    outbox = [dict(r) for r in conn.execute("SELECT * FROM outbox")]
    assert len(outbox) == 1
    assert outbox[0]["status"] == "pending"
    assert outbox[0]["body"] == "Halo"
    [row] = rows(conn, status="done")
    assert "queued in outbox" in row["reason"]
    assert len(roles[1].runs) == 1


async def test_blocked_customer_message_is_rejected(manager, conn, llm, roles):
    set_policy(conn, 1, 10_000, ["customer_message"])
    result = await manager.handle(make_task(TaskKind.CUSTOMER_MESSAGE, payload={"body": "x"}))
    assert result.status == "rejected"
    assert roles[1].runs == []
    assert llm.calls == []
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# LLM failure (Req 4.4)
# --------------------------------------------------------------------------- #
async def test_llm_failure_goes_rule_only_and_role_still_runs(manager, conn, llm, roles):
    llm.fail_next()
    result = await manager.handle(make_task(TaskKind.DRAFT))
    assert result.status == "done"
    assert roles[1].seen_text == [None]
    assert roles[1].seen_rule_only == [True]
    [failure] = rows(conn, status="failed")
    assert failure["reason"] == LLM_DOWN
    assert API_KEY not in failure["reason"]


async def test_llm_failure_on_customer_message_still_queues_outbox(manager, conn, llm, roles):
    llm.fail_next()
    result = await manager.handle(make_task(TaskKind.CUSTOMER_MESSAGE, payload={"body": "Halo"}))
    assert result.status == "done"
    assert roles[1].seen_rule_only == [True]
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert [r["reason"] for r in rows(conn, status="failed")] == [LLM_DOWN]


async def test_rule_only_flag_is_set_on_task(manager, llm):
    llm.fail_next()
    task = make_task(TaskKind.DRAFT)
    await manager.handle(task)
    assert task.rule_only is True


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #
async def test_role_exception_is_contained_and_secret_redacted(manager, conn):
    manager._roles[1] = BoomRole()
    result = await manager.handle(make_task(TaskKind.READ))
    assert result.status == "failed"
    assert API_KEY not in result.message
    [row] = rows(conn, status="failed")
    assert "RuntimeError" in row["reason"]
    assert API_KEY not in row["reason"]


async def test_manager_keeps_working_after_a_failed_task(manager, roles):
    manager._roles[1] = BoomRole()
    await manager.handle(make_task(TaskKind.READ))
    result = await manager.handle(make_task(TaskKind.READ, agent_id=2))
    assert result.status == "done"
    assert len(roles[2].runs) == 1


async def test_unknown_agent_is_a_failed_result(manager, conn):
    result = await manager.handle(make_task(TaskKind.READ, agent_id=99))
    assert result.status == "failed"
    assert len(rows(conn, status="failed")) == 1


# --------------------------------------------------------------------------- #
# Handoffs (Req 4.1)
# --------------------------------------------------------------------------- #
async def drain(sub):
    events = []
    while True:
        try:
            events.append(await asyncio.wait_for(sub.get(), timeout=0.05))
        except asyncio.TimeoutError:
            return events


async def test_assign_records_handoff_and_publishes_event(manager, conn, bus, roles):
    sub = bus.subscribe()
    result = await manager.assign(make_task(TaskKind.READ, agent_id=1), 2)
    assert result.status == "done"
    assert roles[1].runs == [] and len(roles[2].runs) == 1
    [handoff] = rows(conn, action="handoff")
    assert handoff["agent_id"] == 1
    assert "1" in handoff["reason"] and "2" in handoff["reason"]
    events = await drain(sub)
    assert {
        "type": "handoff",
        "from_agent_id": 1,
        "to_agent_id": 2,
    } in events


async def test_assign_to_unknown_agent_fails_without_raising(manager, conn, bus, roles):
    sub = bus.subscribe()
    result = await manager.assign(make_task(TaskKind.READ), 99)
    assert result.status == "failed"
    assert roles[1].runs == [] and roles[2].runs == []
    assert rows(conn, action="handoff", status="failed")
    assert all(e["type"] != "handoff" for e in await drain(sub))


async def test_handoff_spend_is_attributed_to_target_agent(manager, conn):
    await manager.assign(make_task(TaskKind.SPEND, agent_id=1, cost=500), 2)
    agent = conn.execute("SELECT agent_id FROM spend_ledger").fetchone()[0]
    assert agent == 2


# --------------------------------------------------------------------------- #
# Ordering (Req 4.2)
# --------------------------------------------------------------------------- #
async def test_competing_spends_second_is_rejected(manager, conn, roles):
    first = make_task(TaskKind.SPEND, agent_id=1, cost=6_000, reason="first")
    second = make_task(TaskKind.SPEND, agent_id=2, cost=6_000, reason="second")
    r1, r2 = await asyncio.gather(manager.handle(first), manager.handle(second))
    assert (r1.status, r2.status) == ("done", "rejected")
    assert spent_today(conn, 1, FakeClock()) == 6_000
    assert roles[2].runs == []
    assert [r["status"] for r in rows(conn)] == ["done", "rejected"]


async def test_handle_many_runs_in_order(manager, roles):
    tasks = [make_task(TaskKind.READ, reason=f"t{i}") for i in range(4)]
    results = await manager.handle_many(tasks)
    assert [r.status for r in results] == ["done"] * 4
    assert [t.reason for t in roles[1].runs] == ["t0", "t1", "t2", "t3"]


async def test_handle_many_rejects_later_conflicting_spend(manager, conn):
    results = await manager.handle_many(
        [
            make_task(TaskKind.SPEND, cost=7_000),
            make_task(TaskKind.SPEND, cost=7_000),
            make_task(TaskKind.SPEND, cost=3_000),
        ]
    )
    assert [r.status for r in results] == ["done", "rejected", "done"]


class SlowLLM:
    """Yields to the event loop inside complete() so overlap would show up."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []

    async def complete(self, model, messages, max_tokens=400):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(messages[-1]["content"].split("\n")[0])
        await asyncio.sleep(0.01)
        self.active -= 1
        return "text"


async def test_tasks_never_overlap_and_keep_arrival_order(conn, roles):
    slow = SlowLLM()
    manager = Manager(conn, 1, FakeClock(), slow, Settings(model_low=LOW), roles)
    tasks = [make_task(TaskKind.DRAFT, reason=f"t{i}") for i in range(3)]
    await asyncio.gather(*(manager.handle(t) for t in tasks))
    assert slow.max_active == 1
    assert slow.order == ["t0", "t1", "t2"]


async def test_non_llm_error_from_llm_is_contained(conn, roles):
    class BrokenLLM:
        async def complete(self, model, messages, max_tokens=400):
            raise ValueError("bug in client")

    manager = Manager(conn, 1, FakeClock(), BrokenLLM(), Settings(model_low=LOW), roles)
    result = await manager.handle(make_task(TaskKind.DRAFT))
    assert result.status == "failed"
    assert roles[1].runs == []
