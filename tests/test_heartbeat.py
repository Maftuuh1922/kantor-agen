"""Unit tests for the heartbeat (Req 3.1, 3.2, 3.4, 8.1, 8.3). No network, no real waiting."""
import asyncio

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import recent
from skeleton.runtime import (
    ActionResult,
    BaseRole,
    FakeLLMClient,
    Heartbeat,
    Manager,
    Task,
    TaskKind,
    set_agent_paused,
    set_business_paused,
)


class TaskRole(BaseRole):
    """Emits one read task per tick and records the runs."""

    name = "worker"

    def __init__(self, status="done", on_run=None):
        self.status = status
        self.on_run = on_run
        self.trigger_calls = 0
        self.runs: list[Task] = []

    def triggers(self, ctx):
        self.trigger_calls += 1
        return [self.make_task(1)]

    def make_task(self, agent_id):
        return Task(agent_id=agent_id, kind=TaskKind.READ, payload={}, cost_idr=0, reason=f"{self.name} check")

    def run(self, task, ctx):
        self.runs.append(task)
        if self.on_run is not None:
            self.on_run(task, ctx)
        return ActionResult(status=self.status, message=f"{self.name} {self.status}")


class AgentTaskRole(TaskRole):
    def __init__(self, agent_id, **kwargs):
        super().__init__(**kwargs)
        self.agent_id = agent_id
        self.name = f"agent{agent_id}"

    def triggers(self, ctx):
        self.trigger_calls += 1
        return [self.make_task(self.agent_id)]


class BrokenTriggerRole(BaseRole):
    name = "broken"

    def triggers(self, ctx):
        raise RuntimeError("bad trigger")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id in (1, 2, 3):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', 'A', ?)",
            (agent_id, agent_id),
        )
    yield c
    c.close()


def build(conn, roles, bus=None, sleep=None):
    settings = Settings(heartbeat_seconds=7)
    manager = Manager(conn, 1, FakeClock(), FakeLLMClient(), settings, roles, bus=bus)
    return Heartbeat(conn, 1, FakeClock(), manager, roles, settings, bus=bus, sleep=sleep)


def make_roles(**kwargs):
    return {i: AgentTaskRole(i, **kwargs) for i in (1, 2, 3)}


def test_tick_runs_triggers_and_tasks_in_agent_order(conn):
    roles = make_roles()
    hb = build(conn, roles)

    results = asyncio.run(hb.tick())

    assert [r.status for r in results] == ["done", "done", "done"]
    assert [roles[i].trigger_calls for i in (1, 2, 3)] == [1, 1, 1]
    assert [len(roles[i].runs) for i in (1, 2, 3)] == [1, 1, 1]
    agents = [r["agent_id"] for r in reversed(recent(conn, 1, limit=10))]
    assert agents == [1, 2, 3]


def test_paused_business_starts_nothing(conn):
    roles = make_roles()
    hb = build(conn, roles)
    assert set_business_paused(conn, 1, True)

    assert asyncio.run(hb.tick()) == []
    assert all(r.trigger_calls == 0 and r.runs == [] for r in roles.values())
    assert recent(conn, 1) == []

    set_business_paused(conn, 1, False)
    assert len(asyncio.run(hb.tick())) == 3


def test_paused_agent_is_skipped(conn):
    roles = make_roles()
    hb = build(conn, roles)
    set_agent_paused(conn, 2, True)

    results = asyncio.run(hb.tick())

    assert len(results) == 2
    assert roles[2].trigger_calls == 0 and roles[2].runs == []
    assert len(roles[1].runs) == 1 and len(roles[3].runs) == 1


def test_agent_without_role_is_skipped(conn):
    roles = {1: AgentTaskRole(1)}
    hb = build(conn, roles)
    assert len(asyncio.run(hb.tick())) == 1


def test_business_paused_mid_run_stops_later_tasks(conn):
    roles = make_roles()
    roles[1].on_run = lambda task, ctx: set_business_paused(ctx.conn, 1, True)
    hb = build(conn, roles)

    results = asyncio.run(hb.tick())

    assert len(results) == 1  # the running task finished its step
    assert len(roles[1].runs) == 1
    assert roles[2].runs == [] and roles[3].runs == []


def test_agent_paused_mid_run_skips_that_agents_task(conn):
    roles = make_roles()
    roles[1].on_run = lambda task, ctx: set_agent_paused(ctx.conn, 2, True)
    hb = build(conn, roles)

    results = asyncio.run(hb.tick())

    assert len(results) == 2
    assert roles[2].runs == []
    assert len(roles[3].runs) == 1


def test_trigger_exception_does_not_block_other_agents(conn):
    roles = {1: BrokenTriggerRole(), 2: AgentTaskRole(2), 3: AgentTaskRole(3)}
    hb = build(conn, roles)

    results = asyncio.run(hb.tick())

    assert len(results) == 2
    failed = [r for r in recent(conn, 1) if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["agent_id"] == 1
    assert "bad trigger" in failed[0]["reason"]


def test_running_is_set_during_task_and_cleared_after(conn):
    seen = {}
    roles = make_roles()

    def on_run(task, ctx):
        seen["running"] = dict(hb.running)

    roles[1].on_run = on_run
    hb = build(conn, roles)

    asyncio.run(hb.tick())

    assert list(seen["running"]) == [1]
    assert seen["running"][1].agent_id == 1
    assert hb.running == {}


def test_running_cleared_when_task_fails(conn):
    def boom(task, ctx):
        raise RuntimeError("boom")

    roles = make_roles()
    roles[1].on_run = boom
    hb = build(conn, roles)

    results = asyncio.run(hb.tick())

    assert results[0].status == "failed"
    assert hb.running == {}


def drain(bus_events):
    return [e for e in bus_events if e["type"] == "agent_state"]


async def collect(sub):
    events = []
    while True:
        try:
            events.append(await asyncio.wait_for(sub.get(), timeout=0.01))
        except asyncio.TimeoutError:
            return events


def test_agent_state_events_working_then_idle(conn):
    async def scenario():
        bus = EventBus()
        sub = bus.subscribe()
        hb = build(conn, {1: AgentTaskRole(1)}, bus=bus)
        await hb.tick()
        return drain(await collect(sub))

    events = asyncio.run(scenario())

    assert [(e["agent_id"], e["state"]) for e in events] == [(1, "working"), (1, "idle")]
    assert events[0]["bubble_text"] == "agent1 check"


def test_agent_state_waiting_when_result_is_pending(conn):
    async def scenario():
        bus = EventBus()
        sub = bus.subscribe()
        hb = build(conn, {1: AgentTaskRole(1, status="pending")}, bus=bus)
        await hb.tick()
        return drain(await collect(sub))

    events = asyncio.run(scenario())

    assert [e["state"] for e in events] == ["working", "waiting"]
    assert events[1]["bubble_text"] == "agent1 pending"


def test_run_forever_ticks_n_times_then_stops_cleanly(conn):
    roles = {1: AgentTaskRole(1)}
    intervals = []

    async def scenario():
        reached = asyncio.Event()

        async def fake_sleep(seconds):
            intervals.append(seconds)
            if len(intervals) >= 3:
                reached.set()
                await asyncio.Event().wait()  # park until cancelled

        hb = build(conn, roles, sleep=fake_sleep)
        task = hb.start()
        assert hb.start() is task  # second start does not spawn another loop
        await asyncio.wait_for(reached.wait(), timeout=5)
        await hb.stop()
        return task

    task = asyncio.run(scenario())

    assert intervals == [7, 7, 7]  # settings.heartbeat_seconds
    assert len(roles[1].runs) == 3
    assert task.done() and task.cancelled()


def test_run_forever_survives_a_failing_tick(conn):
    calls = []

    async def scenario():
        reached = asyncio.Event()

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                reached.set()
                await asyncio.Event().wait()

        hb = build(conn, {1: AgentTaskRole(1)}, sleep=fake_sleep)
        original = hb.tick
        state = {"n": 0}

        async def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("tick exploded")
            return await original()

        hb.tick = flaky
        hb.start()
        await asyncio.wait_for(reached.wait(), timeout=5)
        await hb.stop()
        return state["n"]

    assert asyncio.run(scenario()) == 2


def test_stop_without_start_is_a_no_op(conn):
    hb = build(conn, {})
    asyncio.run(hb.stop())


def test_controls_reject_non_bool_and_report_unknown_ids(conn):
    with pytest.raises(ValueError):
        set_business_paused(conn, 1, 1)
    assert set_business_paused(conn, 99, True) is False
    assert set_agent_paused(conn, 99, True) is False
    assert set_agent_paused(conn, 1, True) is True
    assert conn.execute("SELECT paused FROM agent WHERE id = 1").fetchone()[0] == 1

class HandoffRole(BaseRole):
    """Agent 1's role: raises a task that belongs to another agent."""
    name = "handoff"
    def __init__(self, handoff_to):
        self.handoff_to = handoff_to
    def triggers(self, ctx):
        return [Task(1, TaskKind.READ, {"handoff_to": self.handoff_to}, 0, "stock is low")]
class RecordingRole(BaseRole):
    name = "receiver"
    def __init__(self):
        self.runs: list[Task] = []
    def run(self, task, ctx):
        self.runs.append(task)
        return ActionResult(status="done", message="ok")
def test_task_with_handoff_to_goes_through_manager_assign(conn):
    bus = EventBus()
    sub = bus.subscribe()
    receiver = RecordingRole()
    hb = build(conn, {1: HandoffRole(2), 2: receiver}, bus=bus)
    results = asyncio.run(hb.tick())
    assert [r.status for r in results] == ["done"]
    assert len(receiver.runs) == 1 and receiver.runs[0].agent_id == 2
    actions = [(r["agent_id"], r["action"]) for r in reversed(recent(conn, 1, limit=10))]
    assert actions == [(1, "Serah tugas"), (2, "Periksa data")]  # labels, task 14.1
    types = []
    while sub._buffer:
        types.append(sub._buffer.popleft())
    assert any(e["type"] == "handoff" and e["from_agent_id"] == 1 and e["to_agent_id"] == 2 for e in types)
@pytest.mark.parametrize("bad_target", ["2", True, None, 2.0])
def test_invalid_handoff_to_is_ignored(conn, bad_target):
    receiver = RecordingRole()
    hb = build(conn, {1: HandoffRole(bad_target), 2: receiver})
    results = asyncio.run(hb.tick())
    assert [r.status for r in results] == ["info"]  # agent 1's own role handled it
    assert receiver.runs == []
    assert [r["action"] for r in recent(conn, 1, limit=10)] == ["Periksa data"]
