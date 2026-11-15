"""Tests for the office REST API, the static front end and the SSE route.

Req 1.3, 1.4, 2.9, 8.1, 8.2, 10.2. No network. The database is in memory.

The connection is opened with ``check_same_thread=False`` because the test
client serves requests on its own thread; the API itself only touches the
connection from that one event loop thread.
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.log.activity import record
from skeleton.office import OfficeContext, VisualTracker, create_app
from skeleton.policy.engine import authorize, set_policy
from skeleton.runtime import Task, TaskKind, set_agent_paused, set_business_paused

FAKE_KEY = "sk-FAKE-do-not-leak-0123456789"


# ---------------------------------------------------------------------- #
# Fixtures and helpers
# ---------------------------------------------------------------------- #
@pytest.fixture
def conn():
    c = db.connect(":memory:", check_same_thread=False)
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung Maju', 'warung')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Other', 'jasa')")
    for agent_id, role, name, desk in ((1, "cashier", "Sari", 0), (2, "stock", "Budi", 1)):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, ?, ?, ?)",
            (agent_id, role, name, desk),
        )
    c.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (10, 2, 'x', 'Z', 0)")
    yield c
    c.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_ctx(conn, bus, clock):
    def build(settings=None, autostart=False, heartbeat=None):
        return OfficeContext(
            conn=conn,
            business_id=1,
            clock=clock,
            bus=bus,
            settings=settings if settings is not None else Settings(llm_api_key=FAKE_KEY),
            heartbeat=heartbeat,
            tracker=VisualTracker(conn, 1, bus, clock),
            autostart=autostart,
        )

    return build


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()


@pytest.fixture
def client(ctx):
    return TestClient(create_app(ctx))


def paused_flags(conn):
    business = conn.execute("SELECT paused FROM business WHERE id = 1").fetchone()[0]
    agents = {r["id"]: r["paused"] for r in conn.execute("SELECT id, paused FROM agent")}
    return business, agents


def spend_task(cost, agent_id=1):
    return Task(agent_id=agent_id, kind=TaskKind.SPEND, payload={}, cost_idr=cost, reason="buy")


def drain(sub):
    events = []
    while sub._buffer:
        events.append(sub._buffer.popleft())
    return events


# ---------------------------------------------------------------------- #
# GET /api/office
# ---------------------------------------------------------------------- #
def test_office_returns_business_agents_policy_and_spend(client, conn, clock):
    set_policy(conn, 1, 50_000, ["customer_message"])
    authorize(conn, 1, spend_task(12_000), clock)
    body = client.get("/api/office").json()
    assert body["business"] == {"id": 1, "name": "Warung Maju", "type": "warung", "paused": False}
    assert body["agents"] == [
        {"id": 1, "name": "Sari", "role": "cashier", "desk_index": 0, "paused": False},
        {"id": 2, "name": "Budi", "role": "stock", "desk_index": 1, "paused": False},
    ]
    assert body["policy"]["daily_limit_idr"] == 50_000
    assert body["policy"]["blocked_actions"] == ["customer_message"]
    assert body["spent_today_idr"] == 12_000
    assert body["demo_mode"] is False


def test_office_without_policy_says_it_is_not_set(client, conn, clock):
    """Req 1.3: no policy yet, and spending is refused until the owner sets one."""
    policy = client.get("/api/office").json()["policy"]
    assert policy["is_set"] is False and policy["daily_limit_idr"] is None
    assert not authorize(conn, 1, spend_task(1), clock).allowed


def test_office_demo_mode_follows_missing_key(make_ctx):
    app = create_app(make_ctx(settings=Settings()))
    assert TestClient(app).get("/api/office").json()["demo_mode"] is True


def test_office_unknown_business_is_404(conn, make_ctx):
    ctx = make_ctx()
    ctx.business_id = 999
    resp = TestClient(create_app(ctx)).get("/api/office")
    assert resp.status_code == 404 and resp.json()["detail"] == "business not found"


# ---------------------------------------------------------------------- #
# GET /api/agents/{id}
# ---------------------------------------------------------------------- #
def test_agent_panel_data(client, conn, clock):
    set_policy(conn, 1, 100_000, [])
    authorize(conn, 1, spend_task(7_000, agent_id=1), clock)
    authorize(conn, 1, spend_task(3_000, agent_id=2), clock)
    record(conn, 1, 1, "spend", "bought rice", 7_000, "done", clock)
    record(conn, 1, 2, "read", "checked stock", 0, "info", clock)
    body = client.get("/api/agents/1").json()
    assert body["id"] == 1 and body["name"] == "Sari" and body["role"] == "cashier"
    assert body["desk_index"] == 0 and body["paused"] is False
    assert body["state"] == "idle" and body["bubble_text"] == ""
    assert body["spent_today_idr"] == 7_000  # this agent only
    assert [a["reason"] for a in body["recent_actions"]] == ["bought rice"]
    assert set(body["recent_actions"][0]) == {
        "id", "action", "reason", "cost_idr", "status", "created_at"
    }


def test_agent_state_is_paused_after_pause(client):
    client.post("/api/agents/1/pause", json={"paused": True})
    assert client.get("/api/agents/1").json()["state"] == "paused"


@pytest.mark.parametrize("agent_id", ["999", "10", "99999999999999999999999"])
def test_agent_unknown_or_of_another_business_is_404(client, agent_id):
    assert client.get(f"/api/agents/{agent_id}").status_code == 404
    assert client.post(f"/api/agents/{agent_id}/pause", json={"paused": True}).status_code == 404


def test_agent_id_must_be_an_integer(client):
    assert client.get("/api/agents/abc").status_code == 422


def test_pausing_another_business_agent_changes_nothing(client, conn):
    client.post("/api/agents/10/pause", json={"paused": True})
    assert conn.execute("SELECT paused FROM agent WHERE id = 10").fetchone()[0] == 0


# ---------------------------------------------------------------------- #
# GET /api/activity, /api/summary, /api/outbox
# ---------------------------------------------------------------------- #
def test_activity_newest_first_and_limit(client, conn, clock):
    for i in range(5):
        clock.advance(1)
        record(conn, 1, 1, "read", f"step {i}", 0, "done", clock)
    record(conn, 2, 10, "read", "other business", 0, "done", clock)
    rows = client.get("/api/activity").json()
    assert [r["reason"] for r in rows] == [f"step {i}" for i in (4, 3, 2, 1, 0)]
    assert [r["reason"] for r in client.get("/api/activity?limit=2").json()] == ["step 4", "step 3"]


@pytest.mark.parametrize("limit", ["0", "-1", "abc", "100000"])
def test_activity_rejects_bad_limit(client, limit):
    assert client.get(f"/api/activity?limit={limit}").status_code == 422


def test_summary_has_todays_numbers_and_pending_outbox(client, conn, clock):
    set_policy(conn, 1, 100_000, [])
    record(conn, 1, 1, "spend", "buy", 5_000, "done", clock)
    record(conn, 1, 2, "read", "check", 0, "pending", clock)
    task = Task(1, TaskKind.CUSTOMER_MESSAGE, {"channel": "wa", "recipient": "08", "body": "hi"}, 0, "msg")
    assert authorize(conn, 1, task, clock).allowed
    body = client.get("/api/summary").json()
    assert body["day"] == clock.today()
    assert body["total_actions"] == 2 and body["total_spent_idr"] == 5_000
    assert body["by_status"] == {"done": 1, "pending": 1}
    assert len(body["pending"]) == 1
    assert body["pending_outbox"] == 1


def test_outbox_lists_pending_messages_read_only(client, conn, clock):
    set_policy(conn, 1, 0, [])
    for body in ("one", "two"):
        task = Task(2, TaskKind.CUSTOMER_MESSAGE, {"channel": "wa", "recipient": "0812", "body": body}, 0, "m")
        authorize(conn, 1, task, clock)
    rows = client.get("/api/outbox").json()
    assert [r["body"] for r in rows] == ["two", "one"]
    assert set(rows[0]) == {"id", "agent_id", "channel", "recipient", "body", "status"}
    assert rows[0]["status"] == "pending" and rows[0]["agent_id"] == 2
    # read only: no write verbs
    assert client.post("/api/outbox", json={}).status_code == 405
    assert client.delete("/api/outbox").status_code == 405


# ---------------------------------------------------------------------- #
# Pause controls (Req 8.1)
# ---------------------------------------------------------------------- #
def test_office_pause_toggles_flag_publishes_event_and_updates_tracker(client, conn, bus):
    sub = bus.subscribe()
    resp = client.post("/api/office/pause", json={"paused": True})
    assert resp.status_code == 200 and resp.json() == {"paused": True}
    assert paused_flags(conn)[0] == 1
    events = drain(sub)
    assert {"type": "pause_changed"} in events
    visual = [e for e in events if e["type"] == "visual_state"]
    assert {e["agent_id"] for e in visual} == {1, 2} and all(e["state"] == "paused" for e in visual)
    assert client.get("/api/office").json()["business"]["paused"] is True

    assert client.post("/api/office/pause", json={"paused": False}).json() == {"paused": False}
    assert paused_flags(conn)[0] == 0
    assert any(e["type"] == "visual_state" and e["state"] == "idle" for e in drain(sub))


def test_agent_pause_toggles_only_that_agent(client, conn, bus):
    sub = bus.subscribe()
    resp = client.post("/api/agents/2/pause", json={"paused": True})
    assert resp.json() == {"id": 2, "paused": True, "business_paused": False}
    assert paused_flags(conn) == (0, {1: 0, 2: 1, 10: 0})
    events = drain(sub)
    assert {"type": "pause_changed"} in events
    assert [(e["agent_id"], e["state"]) for e in events if e["type"] == "visual_state"] == [(2, "paused")]
    resp = client.post("/api/agents/2/pause", json={"paused": False})
    assert resp.json()["paused"] is False and paused_flags(conn)[1][2] == 0


@pytest.mark.parametrize("payload", [{"paused": "true"}, {"paused": 1}, {"paused": None}, {}, {"paused": []}])
def test_pause_rejects_non_bool_and_changes_nothing(client, conn, bus, payload):
    sub = bus.subscribe()
    assert client.post("/api/office/pause", json=payload).status_code == 422
    assert client.post("/api/agents/1/pause", json=payload).status_code == 422
    assert paused_flags(conn) == (0, {1: 0, 2: 0, 10: 0})
    assert drain(sub) == []


def test_pause_rejects_a_non_object_body(client):
    assert client.post("/api/office/pause", content=b"not json").status_code == 422
    assert client.post("/api/office/pause", json=True).status_code == 422


def test_paused_office_flag_is_what_the_heartbeat_reads(client, conn):
    client.post("/api/office/pause", json={"paused": True})
    assert conn.execute("SELECT paused FROM business WHERE id = 1").fetchone()[0] == 1


# ---------------------------------------------------------------------- #
# Policy (Req 1.3, 8.2)
# ---------------------------------------------------------------------- #
def policy_rows(conn):
    return conn.execute("SELECT * FROM policy").fetchall()


def test_put_policy_stores_and_returns_the_policy(client, conn):
    resp = client.put("/api/policy", json={"daily_limit_idr": 75_000, "blocked_actions": ["spend", "spend"]})
    assert resp.status_code == 200
    assert resp.json() == {"daily_limit_idr": 75_000, "blocked_actions": ["spend"], "is_set": True}
    row = conn.execute("SELECT daily_limit_idr, blocked_actions FROM policy WHERE business_id = 1").fetchone()
    assert (row[0], json.loads(row[1])) == (75_000, ["spend"])
    assert client.get("/api/office").json()["policy"]["daily_limit_idr"] == 75_000


def test_policy_change_applies_to_the_next_action(client, conn, clock):
    """Req 8.2: a new limit and a new blocked list are used by the very next authorize."""
    set_policy(conn, 1, 100_000, [])
    assert authorize(conn, 1, spend_task(80_000), clock).allowed

    client.put("/api/policy", json={"daily_limit_idr": 50_000, "blocked_actions": []})
    decision = authorize(conn, 1, spend_task(10_000), clock)
    assert not decision.allowed and decision.reason == "daily spending limit reached"

    client.put("/api/policy", json={"daily_limit_idr": 500_000, "blocked_actions": []})
    assert authorize(conn, 1, spend_task(10_000), clock).allowed

    client.put("/api/policy", json={"daily_limit_idr": 500_000, "blocked_actions": ["spend"]})
    decision = authorize(conn, 1, spend_task(1), clock)
    assert not decision.allowed and decision.reason == "blocked action type"


def test_first_policy_lets_agents_spend(client, conn, clock):
    assert not authorize(conn, 1, spend_task(1_000), clock).allowed
    client.put("/api/policy", json={"daily_limit_idr": 5_000, "blocked_actions": []})
    assert authorize(conn, 1, spend_task(1_000), clock).allowed


@pytest.mark.parametrize(
    "payload",
    [
        {"daily_limit_idr": -1, "blocked_actions": []},
        {"daily_limit_idr": True, "blocked_actions": []},
        {"daily_limit_idr": 10.5, "blocked_actions": []},
        {"daily_limit_idr": 1000.0, "blocked_actions": []},
        {"daily_limit_idr": "1000", "blocked_actions": []},
        {"daily_limit_idr": None, "blocked_actions": []},
        {"daily_limit_idr": 10**30, "blocked_actions": []},
        {"daily_limit_idr": 1000},
        {"blocked_actions": []},
        {"daily_limit_idr": 1000, "blocked_actions": "spend"},
        {"daily_limit_idr": 1000, "blocked_actions": [1]},
        {"daily_limit_idr": 1000, "blocked_actions": [None]},
        {"daily_limit_idr": 1000, "blocked_actions": ["teleport"]},
        {"daily_limit_idr": 1000, "blocked_actions": [], "extra": 1},
    ],
)
def test_put_policy_rejects_invalid_input_and_writes_nothing(client, conn, payload):
    assert client.put("/api/policy", json=payload).status_code == 422
    assert policy_rows(conn) == []


def test_invalid_policy_update_keeps_the_existing_policy(client, conn):
    set_policy(conn, 1, 40_000, ["draft"])
    assert client.put("/api/policy", json={"daily_limit_idr": -5, "blocked_actions": []}).status_code == 422
    row = conn.execute("SELECT daily_limit_idr, blocked_actions FROM policy").fetchone()
    assert (row[0], json.loads(row[1])) == (40_000, ["draft"])


def test_put_policy_for_unknown_business_is_404(make_ctx, conn):
    ctx = make_ctx()
    ctx.business_id = 999
    resp = TestClient(create_app(ctx)).put("/api/policy", json={"daily_limit_idr": 1, "blocked_actions": []})
    assert resp.status_code == 404 and policy_rows(conn) == []


# ---------------------------------------------------------------------- #
# Rename and role change (Req 1.4)
# ---------------------------------------------------------------------- #
def test_agent_can_be_renamed_and_its_role_changed(client, conn, clock):
    record(conn, 1, 1, "read", "old name work", 0, "done", clock)
    resp = client.patch("/api/agents/1", json={"name": "  Sari Baru ", "role": "marketing"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "Sari Baru", "role": "marketing", "desk_index": 0, "paused": False}
    assert client.get("/api/agents/1").json()["name"] == "Sari Baru"
    assert client.patch("/api/agents/1", json={"name": "Only Name"}).json()["role"] == "marketing"
    assert len(client.get("/api/activity").json()) == 1  # the log is untouched


@pytest.mark.parametrize(
    "payload", [{}, {"name": ""}, {"name": "   "}, {"name": 5}, {"role": None, "name": None}, {"desk_index": 3}, {"name": "x" * 500}]
)
def test_agent_patch_rejects_bad_input_and_changes_nothing(client, conn, payload):
    assert client.patch("/api/agents/1", json=payload).status_code == 422
    row = conn.execute("SELECT name, role, desk_index FROM agent WHERE id = 1").fetchone()
    assert tuple(row) == ("Sari", "cashier", 0)


def test_agent_patch_of_unknown_agent_is_404(client):
    assert client.patch("/api/agents/999", json={"name": "x"}).status_code == 404
    assert client.patch("/api/agents/10", json={"name": "x"}).status_code == 404


# ---------------------------------------------------------------------- #
# Secrets (Req 10.2)
# ---------------------------------------------------------------------- #
def test_api_key_never_appears_in_any_response(client, conn, clock):
    set_policy(conn, 1, 10_000, [])
    record(conn, 1, 1, "read", "something", 0, "done", clock)
    bodies = []

    def seen(resp):
        bodies.append(resp.text)
        bodies.append(json.dumps(dict(resp.headers)))
        return resp

    for path in (
        "/api/office", "/api/agents/1", "/api/agents/999", "/api/agents/abc", "/api/activity",
        "/api/summary", "/api/outbox", "/", "/static/style.css", "/api/nothing",
    ):
        seen(client.get(path))
    seen(client.post("/api/office/pause", json={"paused": True}))
    seen(client.post("/api/office/pause", json={"paused": FAKE_KEY}))
    seen(client.post("/api/agents/1/pause", json={"paused": FAKE_KEY}))
    seen(client.put("/api/policy", json={"daily_limit_idr": 1, "blocked_actions": [FAKE_KEY]}))
    seen(client.put("/api/policy", json={"daily_limit_idr": FAKE_KEY, "blocked_actions": []}))
    seen(client.patch("/api/agents/1", json={"name": 7, "role": FAKE_KEY, "extra": FAKE_KEY}))
    assert len(bodies) > 20
    assert not [b for b in bodies if FAKE_KEY in b]
    # Even when the owner sends the key as input, it is not echoed back.
    assert "input" not in client.put("/api/policy", json={"daily_limit_idr": 1, "blocked_actions": [FAKE_KEY]}).text


def test_context_repr_hides_the_key(ctx):
    assert FAKE_KEY not in repr(ctx)


async def test_sse_stream_never_contains_the_key(ctx):
    app = create_app(ctx)
    body = (await first_sse_message(app))["body"].decode()
    assert FAKE_KEY not in body


# ---------------------------------------------------------------------- #
# Front end
# ---------------------------------------------------------------------- #
def test_root_serves_index_html(client):
    resp = client.get("/")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "<title>Kantor Agen</title>" in resp.text


def test_relative_asset_paths_resolve(client):
    assert client.get("/style.css").status_code == 200
    assert client.get("/vendor/three.module.js").status_code == 200
    assert client.get("/assets/manifest.json").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_api_routes_win_over_static_and_unknown_paths_404(client):
    assert client.get("/api/office").headers["content-type"].startswith("application/json")
    assert client.get("/api/nothing").status_code == 404
    assert client.get("/no-such-file.js").status_code == 404


# ---------------------------------------------------------------------- #
# SSE (Req 2.8): driven through ASGI directly, because TestClient waits for
# a response to finish and an event stream never does.
# ---------------------------------------------------------------------- #
async def first_sse_message(app, path="/api/events", wait_for=b"event: snapshot"):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    disconnect = asyncio.Event()
    start: dict = {}
    first: dict = {}

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body" and wait_for in message.get("body", b""):
            first.update(message)
            disconnect.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return {"status": start.get("status"), "headers": dict(start.get("headers", [])), **first}


async def test_sse_first_event_is_a_snapshot_of_every_agent(ctx, bus):
    set_agent_paused(ctx.conn, 2, True)
    message = await first_sse_message(create_app(ctx))
    assert message["status"] == 200
    assert message["headers"][b"content-type"].startswith(b"text/event-stream")
    text = message["body"].decode()
    assert text.startswith("event: snapshot\ndata: ")
    data = json.loads(text.split("data: ", 1)[1].strip())
    assert data["agents"] == [
        {"agent_id": 1, "state": "idle", "target_agent_id": None, "bubble_text": ""},
        {"agent_id": 2, "state": "paused", "target_agent_id": None, "bubble_text": ""},
    ]
    assert bus.subscriber_count == 0  # the subscription is closed after disconnect


# ---------------------------------------------------------------------- #
# Lifespan
# ---------------------------------------------------------------------- #
class StubHeartbeat:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")


def test_lifespan_installs_append_only_triggers(make_ctx, conn, clock):
    record(conn, 1, 1, "read", "x", 0, "done", clock)
    with TestClient(create_app(make_ctx(autostart=False))) as client:
        assert client.get("/api/office").status_code == 200
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM activity")


def test_autostart_false_does_not_start_heartbeat_or_tracker(make_ctx):
    heartbeat = StubHeartbeat()
    ctx = make_ctx(autostart=False, heartbeat=heartbeat)
    with TestClient(create_app(ctx)):
        assert ctx.tracker._task is None
    assert heartbeat.calls == []


def test_autostart_true_starts_and_stops_heartbeat_and_tracker(make_ctx, bus):
    heartbeat = StubHeartbeat()
    ctx = make_ctx(autostart=True, heartbeat=heartbeat)
    with TestClient(create_app(ctx)):
        assert heartbeat.calls == ["start"]
        assert ctx.tracker._task is not None and not ctx.tracker._task.done()
    assert heartbeat.calls == ["start", "stop"]
    assert ctx.tracker._task is None
    assert bus.subscriber_count == 0


# ---------------------------------------------------------------------- #
# Small related changes: pause helpers with a bus, connect option
# ---------------------------------------------------------------------- #
def test_pause_helpers_publish_only_with_a_bus_and_only_on_success(conn, bus):
    sub = bus.subscribe()
    assert set_business_paused(conn, 1, True) and set_agent_paused(conn, 1, True)
    assert drain(sub) == []  # no bus given: nothing published
    assert set_business_paused(conn, 1, False, bus=bus) and set_agent_paused(conn, 1, False, bus=bus)
    assert drain(sub) == [{"type": "pause_changed"}] * 2
    assert not set_business_paused(conn, 999, True, bus=bus) and not set_agent_paused(conn, 999, True, bus=bus)
    assert drain(sub) == []
    with pytest.raises(ValueError):
        set_agent_paused(conn, 1, "yes", bus=bus)
    assert drain(sub) == []


def test_connect_default_stays_thread_checked():
    c = db.connect(":memory:")
    errors = []

    import threading

    def use():
        try:
            c.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            errors.append(exc)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert len(errors) == 1
    c.close()
