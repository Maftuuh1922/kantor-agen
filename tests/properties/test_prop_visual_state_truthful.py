"""Property test: the visual state matches the real state.

**Property 7: Visual state matches real state**
**Validates: Requirements 2.8** (and 11.1, through the generated test coverage)

Self-contained on purpose: no shared conftest, own fixtures and helpers.

Two layers:

* The pure ``visual_state`` function, over random and over exhaustive inputs.
* The real ``Heartbeat`` + ``VisualTracker`` + ``EventBus`` on an in-memory
  SQLite database with fake roles, over random sequences of ticks, pauses,
  handoffs and clock steps. The bus used here hands every event to the tracker
  at the moment it is published, so the test can compare each published
  ``visual_state`` event with ``heartbeat.running`` at that very moment.

Hypothesis tests are sync, so the async code is driven with ``asyncio.run``.
"""
import asyncio
import itertools
from collections import deque
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.office.visual import VisualState, VisualTracker, visual_state
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

PROP_SETTINGS = dict(max_examples=200, deadline=None)

BUSINESS_ID = 1
AGENT_IDS = (1, 2, 3)

# --------------------------------------------------------------------------- #
# Strategies for the pure function
# --------------------------------------------------------------------------- #
# "A running task" is any value that is not None, including falsy ones.
running_tasks = st.one_of(
    st.none(),
    st.builds(SimpleNamespace, reason=st.text(max_size=8)),
    st.sampled_from([0, "", False, ()]),
)
# An agent id of 0 is falsy but is still a pending handoff.
pending_handoffs = st.one_of(st.none(), st.integers(min_value=0, max_value=5))
booleans = st.booleans()


def expected_working(agent_paused, business_paused, running_task, pending_handoff):
    return (
        running_task is not None
        and not agent_paused
        and not business_paused
        and pending_handoff is None
    )


# --------------------------------------------------------------------------- #
# Pure function
# --------------------------------------------------------------------------- #
# Property 7: visual_state is WORKING if and only if there is a running task,
# the agent is not paused, the business is not paused and no handoff is pending.
# Validates: Requirements 2.8
@settings(**PROP_SETTINGS)
@given(
    agent_paused=booleans,
    business_paused=booleans,
    running_task=running_tasks,
    pending_handoff=pending_handoffs,
    waiting=booleans,
)
def test_working_iff_running_and_not_paused_and_no_handoff(
    agent_paused, business_paused, running_task, pending_handoff, waiting
):
    state = visual_state(agent_paused, business_paused, running_task, pending_handoff, waiting)

    assert isinstance(state, VisualState)
    assert (state == VisualState.WORKING) == expected_working(
        agent_paused, business_paused, running_task, pending_handoff
    )


# Property 7, exhaustive: the same statement over the full product of the
# boolean inputs and the None / not-None inputs, so no corner is left to chance.
# Validates: Requirements 2.8
def test_working_iff_holds_over_every_input_combination():
    combos = list(itertools.product((False, True), (False, True), (None, "task"), (None, 0, 2), (False, True)))
    assert len(combos) == 48

    for agent_paused, business_paused, running_task, pending_handoff, waiting in combos:
        state = visual_state(agent_paused, business_paused, running_task, pending_handoff, waiting)
        assert (state == VisualState.WORKING) == expected_working(
            agent_paused, business_paused, running_task, pending_handoff
        ), (agent_paused, business_paused, running_task, pending_handoff, waiting, state)
        if agent_paused or business_paused:
            assert state == VisualState.PAUSED
        elif pending_handoff is not None:
            assert state == VisualState.HANDOFF
        elif running_task is not None:
            assert state == VisualState.WORKING
        elif waiting:
            assert state == VisualState.WAITING
        else:
            assert state == VisualState.IDLE


# Property 7: a paused agent or a paused business is never WORKING, even with a
# running task. Validates: Requirements 2.8
@settings(**PROP_SETTINGS)
@given(
    pause_kind=st.sampled_from(["agent", "business", "both"]),
    running_task=running_tasks,
    pending_handoff=pending_handoffs,
    waiting=booleans,
)
def test_paused_is_never_working(pause_kind, running_task, pending_handoff, waiting):
    agent_paused = pause_kind in ("agent", "both")
    business_paused = pause_kind in ("business", "both")

    state = visual_state(agent_paused, business_paused, running_task, pending_handoff, waiting)

    assert state != VisualState.WORKING
    assert state == VisualState.PAUSED


# Property 7: an agent with no running task is never WORKING.
# Validates: Requirements 2.8
@settings(**PROP_SETTINGS)
@given(
    agent_paused=booleans,
    business_paused=booleans,
    pending_handoff=pending_handoffs,
    waiting=booleans,
)
def test_no_running_task_is_never_working(agent_paused, business_paused, pending_handoff, waiting):
    state = visual_state(agent_paused, business_paused, None, pending_handoff, waiting)

    assert state != VisualState.WORKING


# Property 7: visual_state is pure: the same inputs always give the same output.
# Validates: Requirements 2.8
@settings(**PROP_SETTINGS)
@given(
    agent_paused=booleans,
    business_paused=booleans,
    running_task=running_tasks,
    pending_handoff=pending_handoffs,
    waiting=booleans,
)
def test_visual_state_is_deterministic(agent_paused, business_paused, running_task, pending_handoff, waiting):
    args = (agent_paused, business_paused, running_task, pending_handoff, waiting)

    results = [visual_state(*args) for _ in range(3)]

    assert results[0] is results[1] is results[2]


# --------------------------------------------------------------------------- #
# Integration: real Heartbeat + VisualTracker + EventBus
# --------------------------------------------------------------------------- #
class LiveBus(EventBus):
    """An EventBus that hands every non-``visual_state`` event to the tracker at once.

    ``heartbeat.running`` is then exactly what it was when the event was
    published. Every ``visual_state`` event the tracker publishes in response is
    checked against it, and all of them are kept in ``visual_events``.
    """

    def __init__(self):
        super().__init__()
        self.tracker = None
        self.heartbeat = None
        self.source = None  # the event being handled by the tracker right now
        self.visual_events = []
        self.violations = []

    def publish(self, event):
        super().publish(event)
        if event.get("type") == "visual_state":
            self._check_visual_event(event)
            return
        outer, self.source = self.source, event
        try:
            self.tracker.handle_event(event)
        finally:
            self.source = outer

    def _check_visual_event(self, event):
        self.visual_events.append(event)
        if event["state"] != "working":
            return
        agent_id = event["agent_id"]
        source = self.source or {}
        from_start_event = (
            source.get("type") == "agent_state"
            and source.get("agent_id") == agent_id
            and source.get("state") == "working"
        )
        if agent_id not in self.heartbeat.running and not from_start_event:
            self.violations.append(f"working reported for agent {agent_id} with no running task: {event}")


class ScriptedRole(BaseRole):
    """Emits one read task per tick (when enabled) and finishes with a fixed status.

    Each run takes the next scripted mid-run action, so pauses and handoffs
    happen while the task is running.
    """

    def __init__(self, agent_id, emits, status, mid_ops, apply_op):
        self.name = f"agent{agent_id}"
        self.agent_id = agent_id
        self.emits = emits
        self.status = status
        self.mid_ops = mid_ops
        self.apply_op = apply_op

    def triggers(self, ctx):
        if not self.emits:
            return []
        return [Task(agent_id=self.agent_id, kind=TaskKind.READ, payload={}, cost_idr=0, reason="check stock")]

    def run(self, task, ctx):
        if self.mid_ops:
            self.apply_op(self.mid_ops.popleft())
        return ActionResult(status=self.status, message=f"{self.name} {self.status}")


agent_ids = st.sampled_from(AGENT_IDS)
office_ops = st.one_of(
    st.tuples(st.just("pause_agent"), agent_ids, booleans),
    st.tuples(st.just("pause_business"), booleans),
    st.tuples(st.just("handoff"), agent_ids, agent_ids),
    st.tuples(st.just("advance"), st.integers(min_value=0, max_value=10)),
)
sequence_ops = st.one_of(st.just(("tick",)), office_ops, office_ops)
mid_run_ops = st.one_of(st.none(), office_ops)
role_specs = st.tuples(booleans, st.sampled_from(["done", "pending", "failed"]))


class Office:
    """One real office: database, clock, bus, tracker, manager, heartbeat and roles."""

    def __init__(self, specs, mid_ops):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
        for agent_id in AGENT_IDS:
            self.conn.execute(
                "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', ?, ?)",
                (agent_id, f"Agent{agent_id}", agent_id),
            )
        self.clock = FakeClock()
        self.bus = LiveBus()
        settings_ = Settings(heartbeat_seconds=7)
        mid = deque(mid_ops)
        roles = {
            agent_id: ScriptedRole(agent_id, emits, status, mid, self.apply_mid_op)
            for agent_id, (emits, status) in zip(AGENT_IDS, specs)
        }
        manager = Manager(self.conn, BUSINESS_ID, self.clock, FakeLLMClient(), settings_, roles, bus=self.bus)
        self.heartbeat = Heartbeat(self.conn, BUSINESS_ID, self.clock, manager, roles, settings_, bus=self.bus)
        self.tracker = VisualTracker(self.conn, BUSINESS_ID, self.bus, self.clock, heartbeat=self.heartbeat)
        self.bus.tracker = self.tracker
        self.bus.heartbeat = self.heartbeat
        self.failures = []

    def close(self):
        self.conn.close()

    # -- actions the owner or the Manager can take ---------------------------
    def apply_mid_op(self, op):
        """Apply an op while a task runs, then check the invariants at that moment."""
        if op is not None:
            self.apply(op)
            self.check(f"mid-run {op}")

    def apply(self, op):
        kind = op[0]
        if kind == "pause_agent":
            set_agent_paused(self.conn, op[1], op[2])
            self.bus.publish({"type": "pause_changed"})
        elif kind == "pause_business":
            set_business_paused(self.conn, BUSINESS_ID, op[1])
            self.bus.publish({"type": "pause_changed"})
        elif kind == "handoff":
            self.bus.publish({"type": "handoff", "from_agent_id": op[1], "to_agent_id": op[2]})
        elif kind == "advance":
            self.clock.advance(op[1])
            self.tracker.refresh()

    # -- invariants -----------------------------------------------------------
    def pause_flags(self):
        business = bool(self.conn.execute("SELECT paused FROM business WHERE id = 1").fetchone()[0])
        agents = {
            int(i): bool(p) for i, p in self.conn.execute("SELECT id, paused FROM agent WHERE business_id = 1")
        }
        return business, agents

    def check(self, label):
        """Compare what the tracker reports with the real state, right now."""
        business_paused, agent_paused = self.pause_flags()
        running = self.heartbeat.running
        now = self.clock.now()
        for entry in self.tracker.current():
            agent_id, state = entry["agent_id"], entry["state"]
            has_handoff = self.tracker.agent_context(agent_id, now).handoff_target is not None
            paused = business_paused or agent_paused[agent_id]
            running_now = agent_id in running

            where = f"[{label}] agent {agent_id} state={state} running={running_now} paused={paused} handoff={has_handoff}"
            if state == "working" and not (running_now and not paused and not has_handoff):
                self.failures.append(f"working but not truthful: {where}")
            if running_now and not paused and not has_handoff and state != "working":
                self.failures.append(f"running task not shown as working: {where}")
            if paused and state != "paused":
                self.failures.append(f"paused agent not shown as paused: {where}")
            if has_handoff and not paused and state != "handoff":
                self.failures.append(f"pending handoff not shown: {where}")

    def check_after_task_finished(self, label):
        """With no task running, nobody may be shown as working."""
        assert not self.heartbeat.running
        for entry in self.tracker.current():
            if entry["state"] == "working":
                self.failures.append(f"[{label}] agent {entry['agent_id']} still working after its task finished")


# Property 7: whenever the tracker reports `working` for an agent, a task is
# really running for that agent (or the event is the matching agent_state start
# event); a paused agent or business and a pending handoff are never `working`;
# after a task finishes the tracker never stays `working`.
# Validates: Requirements 2.8
@settings(**PROP_SETTINGS)
@given(
    specs=st.tuples(role_specs, role_specs, role_specs),
    mid_ops=st.lists(mid_run_ops, max_size=6),
    sequence=st.lists(sequence_ops, min_size=1, max_size=12),
)
def test_tracker_never_reports_working_without_a_running_task(specs, mid_ops, sequence):
    office = Office(specs, mid_ops)

    async def drive():
        office.check("start")
        for step, op in enumerate(sequence):
            if op[0] == "tick":
                await office.heartbeat.tick()
                office.check_after_task_finished(f"after tick {step}")
            else:
                office.apply(op)
            office.check(f"step {step} {op}")

    try:
        asyncio.run(drive())
    finally:
        office.close()

    assert office.bus.violations == []
    assert office.failures == []
    for event in office.bus.visual_events:
        assert event["state"] in {s.value for s in VisualState}
