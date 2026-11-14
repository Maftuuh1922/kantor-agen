"""Unit tests for the visual state, the tracker and the SSE stream (Req 2.8). No network."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.office import sse_response
from skeleton.office.sse import event_stream, format_sse
from skeleton.office.visual import (
    HANDOFF_SECONDS,
    VisualState,
    VisualTracker,
    snapshot,
    visual_state,
)
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

TASK = SimpleNamespace(reason="restock")  # any non-None value counts as a running task


# ---------------------------------------------------------------------- #
# visual_state (pure function)
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "agent_paused, business_paused, running, handoff, waiting, expected",
    [
        (True, False, None, None, False, VisualState.PAUSED),
        (False, True, None, None, False, VisualState.PAUSED),
        (True, True, None, None, False, VisualState.PAUSED),
        (False, False, TASK, None, False, VisualState.WORKING),
        (False, False, TASK, 3, False, VisualState.HANDOFF),
        (False, False, TASK, None, True, VisualState.WORKING),  # running beats waiting
        (False, False, None, None, True, VisualState.WAITING),
        (False, False, None, None, False, VisualState.IDLE),
        (False, False, None, 3, False, VisualState.IDLE),  # a handoff needs a running task
    ],
)
def test_priority_rules(agent_paused, business_paused, running, handoff, waiting, expected):
    assert visual_state(agent_paused, business_paused, running, handoff, waiting) is expected


@pytest.mark.parametrize("agent_paused, business_paused", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("handoff", [None, 2])
@pytest.mark.parametrize("waiting", [False, True])
def test_paused_with_running_task_is_never_working(agent_paused, business_paused, handoff, waiting):
    assert visual_state(agent_paused, business_paused, TASK, handoff, waiting) is VisualState.PAUSED


def test_state_values_are_the_wire_strings():
    assert [s.value for s in VisualState] == ["working", "handoff", "waiting", "idle", "paused"]
    assert VisualState.WORKING == "working"


# ---------------------------------------------------------------------- #
# Fixtures and helpers
# ---------------------------------------------------------------------- #
@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    for agent_id, name in ((1, "Sari"), (2, "Budi"), (3, "Dewi")):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, 'r', ?, ?)",
            (agent_id, name, agent_id),
        )
    yield c
    c.close()


def by_agent(entries):
    return {e["agent_id"]: e for e in entries}


def collect(bus, event_type="visual_state"):
    """Subscribe now; later ``drain`` returns the events of one type seen since."""
    sub = bus.subscribe()

    def drain():
        events = []
        while sub._buffer:
            event = sub._buffer.popleft()
            if event["type"] == event_type:
                events.append(event)
        return events

    return drain


async def next_event(sub, event_type, timeout=2.0):
    async def wait():
        while True:
            event = await sub.get()
            if event["type"] == event_type:
                return event

    return await asyncio.wait_for(wait(), timeout)


def agent_state(agent_id, state, bubble=""):
    return {"type": "agent_state", "agent_id": agent_id, "state": state, "bubble_text": bubble}


def handoff(source, target):
    return {"type": "handoff", "from_agent_id": source, "to_agent_id": target}


# ---------------------------------------------------------------------- #
# snapshot
# ---------------------------------------------------------------------- #
def test_snapshot_lists_every_agent_idle_by_default(conn):
    entries = snapshot(conn, 1, None, FakeClock())
    assert entries == [
        {"agent_id": i, "state": "idle", "target_agent_id": None, "bubble_text": ""} for i in (1, 2, 3)
    ]


def test_snapshot_uses_heartbeat_running_and_pause_flags(conn):
    heartbeat = SimpleNamespace(running={1: TASK, 2: TASK})
    set_agent_paused(conn, 2, True)

    entries = by_agent(snapshot(conn, 1, heartbeat, FakeClock()))

    assert entries[1] == {"agent_id": 1, "state": "working", "target_agent_id": None, "bubble_text": "restock"}
    assert entries[2]["state"] == "paused"  # paused agent with a running task is paused
    assert entries[3]["state"] == "idle"


def test_snapshot_business_pause_pauses_everyone(conn):
    heartbeat = SimpleNamespace(running={1: TASK})
    set_business_paused(conn, 1, True)

    assert {e["state"] for e in snapshot(conn, 1, heartbeat, FakeClock())} == {"paused"}


def test_snapshot_unknown_business_has_no_agents(conn):
    assert snapshot(conn, 99, None, FakeClock()) == []


def test_snapshot_only_returns_agents_of_this_business(conn):
    conn.execute("INSERT INTO business (id, name, type) VALUES (2, 'Jasa', 'jasa')")
    conn.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (9, 2, 'r', 'X', 0)")

    assert [e["agent_id"] for e in snapshot(conn, 1, None, FakeClock())] == [1, 2, 3]


def test_snapshot_includes_waiting_and_handoff_from_tracker(conn):
    clock = FakeClock()
    tracker = VisualTracker(conn, 1, EventBus(), clock)
    tracker.handle_event(agent_state(1, "waiting", "needs your OK"))
    tracker.handle_event(agent_state(2, "working", "checking stock"))
    tracker.handle_event(handoff(2, 3))

    entries = by_agent(snapshot(conn, 1, None, clock, tracker=tracker))

    assert entries[1] == {"agent_id": 1, "state": "waiting", "target_agent_id": None, "bubble_text": "needs your OK"}
    assert entries[2]["state"] == "handoff"
    assert entries[2]["target_agent_id"] == 3
    assert "Dewi" in entries[2]["bubble_text"]
    assert entries[3]["state"] == "idle"


# ---------------------------------------------------------------------- #
# Tracker: events in, normalized visual_state events out
# ---------------------------------------------------------------------- #
def test_tracker_publishes_on_agent_state_changes_only(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    drain = collect(bus)

    tracker.handle_event(agent_state(1, "working", "restock"))
    tracker.handle_event(agent_state(1, "working", "restock"))  # no change, no event
    tracker.handle_event(agent_state(1, "idle"))

    assert drain() == [
        {"type": "visual_state", "agent_id": 1, "state": "working", "target_agent_id": None, "bubble_text": "restock"},
        {"type": "visual_state", "agent_id": 1, "state": "idle", "target_agent_id": None, "bubble_text": ""},
    ]
    assert by_agent(tracker.current())[1]["state"] == "idle"


def test_tracker_ignores_bad_and_unrelated_events(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    drain = collect(bus)

    tracker.handle_event({"type": "agent_state", "agent_id": "1", "state": "working"})
    tracker.handle_event({"type": "agent_state", "agent_id": 1, "state": "dancing"})
    tracker.handle_event({"type": "handoff", "from_agent_id": 1})
    tracker.handle_event({"type": "activity", "agent_id": 1})

    assert drain() == []
    assert {e["state"] for e in tracker.current()} == {"idle"}


def test_tracker_handoff_sets_target_and_ends_on_next_agent_state(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    tracker.handle_event(agent_state(1, "working", "restock"))
    drain = collect(bus)

    tracker.handle_event(handoff(1, 2))
    tracker.handle_event(agent_state(1, "idle"))

    events = drain()
    assert [(e["state"], e["target_agent_id"]) for e in events] == [("handoff", 2), ("idle", None)]


def test_handoff_expires_after_eight_seconds(conn):
    clock = FakeClock()
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, clock)
    tracker.handle_event(agent_state(1, "working", "restock"))
    tracker.handle_event(handoff(1, 2))
    drain = collect(bus)
    assert HANDOFF_SECONDS == 8

    clock.advance(7.9)
    tracker.refresh()
    assert by_agent(tracker.current())[1]["state"] == "handoff"
    assert drain() == []

    clock.advance(0.1)
    # current() already sees the expiry; refresh() publishes it.
    assert by_agent(tracker.current())[1]["state"] == "working"
    tracker.refresh()
    assert [(e["state"], e["target_agent_id"]) for e in drain()] == [("working", None)]


def test_tracker_reacts_to_pause_changes(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    tracker.handle_event(agent_state(1, "working", "restock"))
    drain = collect(bus)

    set_agent_paused(conn, 1, True)
    tracker.handle_event({"type": "pause_changed"})
    assert [(e["agent_id"], e["state"]) for e in drain()] == [(1, "paused")]

    set_agent_paused(conn, 1, False)
    tracker.handle_event({"type": "pause_changed"})
    assert [(e["agent_id"], e["state"]) for e in drain()] == [(1, "working")]

    set_business_paused(conn, 1, True)
    tracker.refresh()
    assert [(e["agent_id"], e["state"]) for e in drain()] == [(1, "paused"), (2, "paused"), (3, "paused")]


def test_tracker_starts_from_tasks_already_running(conn):
    heartbeat = SimpleNamespace(running={2: TASK})

    tracker = VisualTracker(conn, 1, EventBus(), FakeClock(), heartbeat=heartbeat)

    assert by_agent(tracker.current())[2]["state"] == "working"


class OneTaskRole(BaseRole):
    name = "worker"

    def __init__(self, status="done"):
        self.status = status

    def triggers(self, ctx):
        return [Task(agent_id=1, kind=TaskKind.READ, payload={}, cost_idr=0, reason="check stock")]

    def run(self, task, ctx):
        return ActionResult(status=self.status, message="result text")


async def test_tracker_follows_real_heartbeat_events(conn):
    clock, bus = FakeClock(), EventBus()
    settings = Settings(heartbeat_seconds=7)
    roles = {1: OneTaskRole("pending")}
    manager = Manager(conn, 1, clock, FakeLLMClient(), settings, roles, bus=bus)
    heartbeat = Heartbeat(conn, 1, clock, manager, roles, settings, bus=bus)
    tracker = VisualTracker(conn, 1, bus, clock, heartbeat=heartbeat)
    watcher = bus.subscribe()
    tracker.start()
    try:
        await heartbeat.tick()

        first = await next_event(watcher, "visual_state")
        second = await next_event(watcher, "visual_state")
    finally:
        await tracker.stop()

    assert (first["agent_id"], first["state"], first["bubble_text"]) == (1, "working", "check stock")
    assert (second["agent_id"], second["state"], second["bubble_text"]) == (1, "waiting", "result text")
    assert by_agent(tracker.current())[1]["state"] == "waiting"


async def test_tracker_task_handles_handoff_from_bus_and_unsubscribes_on_stop(conn):
    clock, bus = FakeClock(), EventBus()
    tracker = VisualTracker(conn, 1, bus, clock)
    watcher = bus.subscribe()
    tracker.start()
    assert bus.subscriber_count == 2

    bus.publish(agent_state(1, "working", "restock"))
    await next_event(watcher, "visual_state")
    bus.publish(handoff(1, 2))
    event = await next_event(watcher, "visual_state")
    await tracker.stop()

    assert (event["state"], event["target_agent_id"]) == ("handoff", 2)
    assert bus.subscriber_count == 1  # only the watcher is left


async def test_tracker_task_expires_handoff_without_new_events(conn):
    clock, bus = FakeClock(), EventBus()
    tracker = VisualTracker(conn, 1, bus, clock, poll_seconds=0.01)
    watcher = bus.subscribe()
    tracker.start()
    try:
        bus.publish(agent_state(1, "working", "restock"))
        bus.publish(handoff(1, 2))
        await next_event(watcher, "visual_state")
        assert (await next_event(watcher, "visual_state"))["state"] == "handoff"

        clock.advance(HANDOFF_SECONDS)
        event = await next_event(watcher, "visual_state")
    finally:
        await tracker.stop()

    assert event["state"] == "working"


# ---------------------------------------------------------------------- #
# SSE
# ---------------------------------------------------------------------- #
def parse_frame(frame):
    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").split("\n")
    assert lines[0].startswith("event: ") and lines[1].startswith("data: ") and len(lines) == 2
    return lines[0][len("event: "):], json.loads(lines[1][len("data: "):])


def test_format_sse_frame():
    assert format_sse("x", {"a": 1}) == 'event: x\ndata: {"a": 1}\n\n'


async def test_stream_sends_snapshot_first_then_live_events(conn):
    clock, bus = FakeClock(), EventBus()
    tracker = VisualTracker(conn, 1, bus, clock)
    tracker.handle_event(agent_state(1, "working", "restock"))
    stream = event_stream(bus, tracker, keepalive_seconds=30)
    try:
        name, data = parse_frame(await anext(stream))
        assert name == "snapshot"
        assert by_agent(data["agents"])[1]["state"] == "working"
        assert [a["agent_id"] for a in data["agents"]] == [1, 2, 3]

        entry = {"agent_id": 2, "state": "waiting", "target_agent_id": None, "bubble_text": "ok?"}
        bus.publish({"type": "agent_state", "agent_id": 2, "state": "waiting"})  # not forwarded
        bus.publish({"type": "visual_state", **entry})
        record(conn, 1, 2, "read", "looked at stock", 0, "done", clock, bus)

        assert parse_frame(await anext(stream)) == ("visual_state", {"type": "visual_state", **entry})
        name, data = parse_frame(await anext(stream))
        assert name == "activity"
        assert (data["agent_id"], data["action"], data["reason"]) == (2, "read", "looked at stock")
    finally:
        await stream.aclose()


async def test_stream_sends_keepalive_when_quiet(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    stream = event_stream(bus, tracker, keepalive_seconds=0.02)
    try:
        await anext(stream)  # snapshot
        assert await asyncio.wait_for(anext(stream), 1) == ": keepalive\n\n"
        assert await asyncio.wait_for(anext(stream), 1) == ": keepalive\n\n"
    finally:
        await stream.aclose()


async def test_keepalive_is_not_postponed_by_events_that_are_not_forwarded(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    stream = event_stream(bus, tracker, keepalive_seconds=0.05)
    await anext(stream)

    async def noise():
        for _ in range(20):
            bus.publish({"type": "handoff", "from_agent_id": 1, "to_agent_id": 2})
            await asyncio.sleep(0.01)

    noisy = asyncio.create_task(noise())
    try:
        assert await asyncio.wait_for(anext(stream), 1) == ": keepalive\n\n"
    finally:
        noisy.cancel()
        await stream.aclose()


async def test_stream_closes_its_subscription_on_cancel(conn):
    bus = EventBus()
    tracker = VisualTracker(conn, 1, bus, FakeClock())
    received = []

    async def consume():
        async for frame in event_stream(bus, tracker, keepalive_seconds=30):
            received.append(frame)

    task = asyncio.create_task(consume())
    for _ in range(20):
        await asyncio.sleep(0)
        if received:
            break
    assert received and bus.subscriber_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bus.subscriber_count == 0


async def test_stream_closes_its_subscription_on_aclose(conn):
    bus = EventBus()
    stream = event_stream(bus, VisualTracker(conn, 1, bus, FakeClock()))
    await anext(stream)
    assert bus.subscriber_count == 1

    await stream.aclose()

    assert bus.subscriber_count == 0


async def test_stream_stops_when_the_client_disconnects(conn):
    bus = EventBus()

    class Request:
        disconnected = False

        async def is_disconnected(self):
            return self.disconnected

    request = Request()
    stream = event_stream(bus, VisualTracker(conn, 1, bus, FakeClock()), request, keepalive_seconds=30)
    await anext(stream)
    request.disconnected = True
    bus.publish({"type": "visual_state", "agent_id": 1, "state": "idle", "target_agent_id": None, "bubble_text": ""})

    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert bus.subscriber_count == 0


async def test_sse_response_headers_and_media_type(conn):
    bus = EventBus()

    response = sse_response(bus, VisualTracker(conn, 1, bus, FakeClock()))

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"].startswith("text/event-stream")
    await response.body_iterator.aclose()
