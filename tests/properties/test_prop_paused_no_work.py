"""Property test: paused means no new work.

Self-contained on purpose: no shared conftest, own fixtures and helpers.

Real ``Heartbeat`` and ``Manager`` run on an in-memory SQLite database with a
``FakeClock`` and a ``FakeLLMClient``. The roles are fakes that write every call
to one ordered log, so the tests can check what started and in which order.
Hypothesis tests are sync, so the async code is driven with ``asyncio.run``.
"""
import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.policy.engine import set_policy
from skeleton.runtime import (
    ActionResult,
    FakeLLMClient,
    Heartbeat,
    Manager,
    Task,
    TaskKind,
    set_agent_paused,
    set_business_paused,
)

BUSINESS_ID = 1
BUSINESS_TARGET = 0  # pause-event target meaning "the whole business"
DAILY_LIMIT_IDR = 100_000_000  # far above anything the strategy can spend
MAX_COST_IDR = 50_000
MAX_AGENTS = 5
MAX_TASKS_PER_AGENT = 3
KINDS = (TaskKind.READ, TaskKind.DRAFT, TaskKind.SPEND, TaskKind.CUSTOMER_MESSAGE)

PROP_SETTINGS = dict(max_examples=200, deadline=None)


# --------------------------------------------------------------------------- #
# Scenario strategy
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    n_agents: int
    business_paused: bool
    agent_paused: dict[int, bool]
    # agent_id -> list of (kind, cost_idr) the agent's role emits in triggers()
    specs: dict[int, list[tuple[str, int]]]
    # (step, target): when the run with global index `step` executes, it sets the
    # target paused. target 0 = business, 1..n = that agent.
    events: list[tuple[int, int]] = field(default_factory=list)


@st.composite
def scenarios(draw, business_paused=None, with_events=True):
    n = draw(st.integers(min_value=1, max_value=MAX_AGENTS))
    agent_ids = list(range(1, n + 1))
    if business_paused is None:
        business_paused = draw(st.booleans())
    agent_paused = {a: draw(st.booleans()) for a in agent_ids}
    specs = {}
    for a in agent_ids:
        count = draw(st.integers(min_value=0, max_value=MAX_TASKS_PER_AGENT))
        items = []
        for _ in range(count):
            kind = draw(st.sampled_from(KINDS))
            cost = draw(st.integers(min_value=1, max_value=MAX_COST_IDR)) if kind == TaskKind.SPEND else 0
            items.append((kind, cost))
        specs[a] = items
    events = []
    if with_events:
        events = draw(
            st.lists(
                st.tuples(
                    st.integers(min_value=0, max_value=n * MAX_TASKS_PER_AGENT),
                    st.integers(min_value=0, max_value=n),
                ),
                max_size=3,
            )
        )
    return Scenario(n, business_paused, agent_paused, specs, events)


# --------------------------------------------------------------------------- #
# Fakes and setup
# --------------------------------------------------------------------------- #
class World:
    """Shared ordered call log and the pause events a run may fire."""

    def __init__(self, events):
        self.log: list[tuple] = []
        self.events = list(events)
        self.next_step = 0
        self.conn: sqlite3.Connection | None = None

    def reset(self, events=()):
        self.log = []
        self.events = list(events)
        self.next_step = 0


class RecordingRole:
    """Emits the scenario's tasks, logs every call, and may pause things during run()."""

    def __init__(self, agent_id: int, specs: list[tuple[str, int]], world: World) -> None:
        self.name = f"agent{agent_id}"
        self.agent_id = agent_id
        self.specs = specs
        self.world = world

    def triggers(self, ctx):
        self.world.log.append(("triggers", self.agent_id))
        return [
            Task(
                agent_id=self.agent_id,
                kind=kind,
                payload={},
                cost_idr=cost,
                reason=f"a{self.agent_id}-t{i}",
            )
            for i, (kind, cost) in enumerate(self.specs)
        ]

    def run(self, task, ctx):
        step = self.world.next_step
        self.world.next_step += 1
        self.world.log.append(("run", self.agent_id, task.reason))
        for event_step, target in self.world.events:
            if event_step != step:
                continue
            if target == BUSINESS_TARGET:
                assert set_business_paused(ctx.conn, BUSINESS_ID, True)
            else:
                assert set_agent_paused(ctx.conn, target, True)
            self.world.log.append(("pause", target))
        return ActionResult(status="done", message=f"{self.name} ran {task.reason}")


def build_office(scn: Scenario, events=()):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (?, 'Warung', 'warung')", (BUSINESS_ID,))
    for a in range(1, scn.n_agents + 1):
        conn.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, ?, 'r', 'A', ?)",
            (a, BUSINESS_ID, a),
        )
    set_policy(conn, BUSINESS_ID, DAILY_LIMIT_IDR, [])
    set_business_paused(conn, BUSINESS_ID, scn.business_paused)
    for a, flag in scn.agent_paused.items():
        set_agent_paused(conn, a, flag)

    world = World(events)
    world.conn = conn
    roles = {a: RecordingRole(a, scn.specs[a], world) for a in range(1, scn.n_agents + 1)}
    settings_obj = Settings(heartbeat_seconds=7)
    clock = FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))  # 10:00 in Jakarta
    manager = Manager(conn, BUSINESS_ID, clock, FakeLLMClient(), settings_obj, roles)
    heartbeat = Heartbeat(conn, BUSINESS_ID, clock, manager, roles, settings_obj, sleep=_no_sleep)
    return conn, world, heartbeat


async def _no_sleep(_seconds):
    return None


def activity_rows(conn):
    return conn.execute("SELECT agent_id, action, status FROM activity ORDER BY id").fetchall()


def runs_in(log):
    return [e[2] for e in log if e[0] == "run"]


def all_task_reasons(scn: Scenario, only_agents=None):
    """Reasons of every task in agent order, optionally limited to some agents."""
    return [
        f"a{a}-t{i}"
        for a in range(1, scn.n_agents + 1)
        if only_agents is None or a in only_agents
        for i in range(len(scn.specs[a]))
    ]


def assert_no_start_while_paused(log, scn: Scenario):
    """Walk the ordered log: a run must never start while its agent or the business is paused."""
    biz = scn.business_paused
    paused = {a for a, flag in scn.agent_paused.items() if flag}
    for entry in log:
        if entry[0] == "pause":
            if entry[1] == BUSINESS_TARGET:
                biz = True
            else:
                paused.add(entry[1])
        elif entry[0] == "run":
            assert not biz, f"task {entry[2]} started while the business was paused: {log}"
            assert entry[1] not in paused, f"task {entry[2]} started while agent {entry[1]} was paused: {log}"
        elif entry[0] == "triggers":
            assert not biz, f"triggers called while the business was paused: {log}"
            assert entry[1] not in paused, f"triggers called for paused agent {entry[1]}: {log}"


def expected_runs_with_events(scn: Scenario) -> list[str]:
    """Reference model of the contract: pause is read before every task step."""
    if scn.business_paused:
        return []
    biz = False
    paused = {a for a, flag in scn.agent_paused.items() if flag}
    queue = [
        (a, f"a{a}-t{i}")
        for a in range(1, scn.n_agents + 1)
        if a not in paused
        for i in range(len(scn.specs[a]))
    ]
    out: list[str] = []
    step = 0
    for agent_id, reason in queue:
        if biz:
            break
        if agent_id in paused:
            continue
        out.append(reason)
        for event_step, target in scn.events:
            if event_step == step:
                if target == BUSINESS_TARGET:
                    biz = True
                else:
                    paused.add(target)
        step += 1
    return out


async def unpause_all_and_tick(scn: Scenario, conn, world: World, heartbeat: Heartbeat):
    """Resume everything, drop the pause events, and run one more tick."""
    assert set_business_paused(conn, BUSINESS_ID, False)
    for a in range(1, scn.n_agents + 1):
        assert set_agent_paused(conn, a, False)
    world.reset()
    return await heartbeat.tick()


def assert_works_normally_after_unpause(scn: Scenario, conn, world: World, results):
    expected = all_task_reasons(scn)
    assert runs_in(world.log) == expected
    assert len(results) == len(expected)
    assert all(r.status == "done" for r in results)
    assert [e[1] for e in world.log if e[0] == "triggers"] == list(range(1, scn.n_agents + 1))


# --------------------------------------------------------------------------- #
# Property 6 (a): business paused before the tick
# --------------------------------------------------------------------------- #
# Property 6: Paused means no new work.
# For any state where the business is paused, the heartbeat starts zero new tasks:
# tick() returns [], no role.triggers or role.run is called, and no activity row is
# written. After unpausing, the next tick runs work normally.
# Validates: Requirements 3.4, 8.3, 11.1
@settings(**PROP_SETTINGS)
@given(scn=scenarios(business_paused=True))
def test_business_paused_before_tick_starts_nothing(scn):
    conn, world, heartbeat = build_office(scn)
    try:

        async def drive():
            paused_results = await heartbeat.tick()
            snapshot = (list(world.log), activity_rows(conn), dict(heartbeat.running))
            resumed_results = await unpause_all_and_tick(scn, conn, world, heartbeat)
            return paused_results, snapshot, resumed_results

        paused_results, (log, rows, running), resumed_results = asyncio.run(drive())

        assert paused_results == []
        assert log == []  # no triggers() and no run() call
        assert rows == []  # no activity row written
        assert running == {}
        assert_works_normally_after_unpause(scn, conn, world, resumed_results)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Property 6 (b): paused agents are skipped, other agents keep working
# --------------------------------------------------------------------------- #
# Property 6: Paused means no new work.
# A paused agent never has its triggers evaluated or its tasks started, while the
# tasks of agents that are not paused still run, in agent order. After unpausing,
# the next tick runs every agent's work normally.
# Validates: Requirements 3.4, 8.3, 11.1
@settings(**PROP_SETTINGS)
@given(scn=scenarios(business_paused=False, with_events=False))
def test_paused_agent_never_starts_while_others_run(scn):
    conn, world, heartbeat = build_office(scn)
    try:
        paused_ids = {a for a, flag in scn.agent_paused.items() if flag}
        active_ids = set(range(1, scn.n_agents + 1)) - paused_ids

        async def drive():
            results = await heartbeat.tick()
            snapshot = (list(world.log), activity_rows(conn))
            resumed_results = await unpause_all_and_tick(scn, conn, world, heartbeat)
            return results, snapshot, resumed_results

        results, (log, rows), resumed_results = asyncio.run(drive())

        assert_no_start_while_paused(log, scn)
        # Nothing of a paused agent was started or logged.
        assert not any(e[1] in paused_ids for e in log)
        assert not any(agent_id in paused_ids for agent_id, _action, _status in rows)
        # Every other agent still got its triggers evaluated and its tasks run.
        assert [e[1] for e in log if e[0] == "triggers"] == sorted(active_ids)
        assert runs_in(log) == all_task_reasons(scn, only_agents=active_ids)
        assert len(results) == len(runs_in(log))
        assert all(r.status == "done" for r in results)
        assert_works_normally_after_unpause(scn, conn, world, resumed_results)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Property 6 (c), (d): a pause set mid-run stops later starts and never raises
# --------------------------------------------------------------------------- #
# Property 6: Paused means no new work.
# When the owner pauses the business or an agent while the heartbeat is running,
# the step in progress finishes and no task STARTS after the pause is set. Pausing
# never raises, and no task fails because of it. After unpausing, the next tick
# runs work normally.
# Validates: Requirements 3.4, 8.3, 11.1
@settings(**PROP_SETTINGS)
@given(scn=scenarios())
def test_pause_set_mid_run_stops_later_tasks(scn):
    conn, world, heartbeat = build_office(scn, events=scn.events)
    try:

        async def drive():
            results = await heartbeat.tick()  # (d): must not raise
            snapshot = (list(world.log), activity_rows(conn), dict(heartbeat.running))
            resumed_results = await unpause_all_and_tick(scn, conn, world, heartbeat)
            return results, snapshot, resumed_results

        results, (log, rows, running), resumed_results = asyncio.run(drive())

        # (c) no task starts after the pause that covers it was set.
        assert_no_start_while_paused(log, scn)
        # The tasks that did start are exactly the ones the contract allows.
        assert runs_in(log) == expected_runs_with_events(scn)
        # The step in progress finished: one result and one activity row per started task.
        assert len(results) == len(runs_in(log))
        assert len(rows) == len(runs_in(log))
        # (d) pausing made nothing fail.
        assert all(r.status == "done" for r in results)
        assert all(status == "done" for _a, _action, status in rows)
        assert running == {}
        # (e) after unpausing, the next tick works normally.
        assert_works_normally_after_unpause(scn, conn, world, resumed_results)
    finally:
        conn.close()
