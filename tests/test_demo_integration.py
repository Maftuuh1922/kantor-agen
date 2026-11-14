"""Checkpoint 9: the skeleton demo with the real backend, end to end.

Requirements 2.5, 2.6, 2.7, 2.8, 3.2, 5.4, 7.1.

``build_demo`` wires the real database, policy engine, Manager, heartbeat,
visual tracker and API around four DUMMY roles. The app is created with
``autostart=False``; the test drives the heartbeat ticks by hand and reads the
results through the HTTP API (``/api/office``, ``/api/agents/{id}``,
``/api/activity``, ``/api/outbox``) and the SSE snapshot. The dummy roles
"work" through a gate, so a task can be held half way and the state read while
it runs. No network, no real waiting.
"""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from skeleton.office import create_app
from skeleton.office.demo import DAILY_LIMIT_IDR, DEMO_STAFF, HOST, build_demo, seed_demo
from skeleton.core import db

TIMEOUT = 5.0
LAP_TICKS = 6  # the longest dummy plan (staff, admin, finance) has 6 entries


class Gate:
    """Replaces ``asyncio.sleep`` in the dummy roles.

    Manual: every "work" parks at the gate until the test lets it go, so the
    state can be read while the task runs. Not manual: it only yields.
    """

    def __init__(self) -> None:
        self.manual = True
        self.entered: asyncio.Queue[float] = asyncio.Queue()
        self.proceed: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        if not self.manual:
            await asyncio.sleep(0)
            return
        await self.entered.put(seconds)
        await self.proceed.get()

    async def arrived(self) -> float:
        return await asyncio.wait_for(self.entered.get(), TIMEOUT)

    def release(self) -> None:
        self.proceed.put_nowait(None)


@pytest.fixture
def gate():
    return Gate()


@pytest.fixture
async def ctx(gate):
    context = build_demo(":memory:", autostart=False, sleep=gate.sleep)
    context.tracker.start()
    yield context
    await context.tracker.stop()
    context.conn.close()


@pytest.fixture
async def client(ctx):
    app = create_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as c:
        c.app = app
        yield c


@pytest.fixture
async def ids(client):
    agents = (await client.get("/api/office")).json()["agents"]
    return {a["name"]: a["id"] for a in agents}


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
async def sse_snapshot(app) -> dict[int, dict]:
    """The first SSE frame (the snapshot) as {agent_id: entry}, read through ASGI."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"demo")],
        "client": ("127.0.0.1", 1234),
        "server": ("demo", 80),
    }
    disconnect = asyncio.Event()
    frames: list[bytes] = []

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"event: snapshot" in message.get("body", b""):
            frames.append(message["body"])
            disconnect.set()

    await asyncio.wait_for(app(scope, receive, send), TIMEOUT)
    data = json.loads(frames[0].decode().split("data: ", 1)[1].strip())
    return {e["agent_id"]: e for e in data["agents"]}


async def agent_view(client, agent_id) -> dict:
    response = await client.get(f"/api/agents/{agent_id}")
    assert response.status_code == 200
    return response.json()


async def wait_state(client, agent_id, state) -> dict:
    """Poll the agent endpoint until it reports ``state`` (the tracker runs as a task)."""
    body: dict = {}
    for _ in range(int(TIMEOUT / 0.01)):
        body = await agent_view(client, agent_id)
        if body["state"] == state:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"agent {agent_id} never became {state!r}, last body: {body}")


async def settle(client, expected: dict[int, str]) -> None:
    """Wait until every agent reports the expected state."""
    for agent_id, state in expected.items():
        await wait_state(client, agent_id, state)


async def check_consistent(client, observed: set[str]) -> dict[int, str]:
    """/api/agents/{id} and the SSE snapshot must tell the same story. Returns the states."""
    snapshot = await sse_snapshot(client.app)
    office = (await client.get("/api/office")).json()
    states: dict[int, str] = {}
    for agent in office["agents"]:
        view = await agent_view(client, agent["id"])
        entry = snapshot[agent["id"]]
        assert view["state"] == entry["state"]
        assert view["bubble_text"] == entry["bubble_text"]
        assert view["paused"] == agent["paused"]  # the same flag, read two ways
        states[agent["id"]] = view["state"]
        observed.add(view["state"])
    return states


async def tick_auto(ctx, gate, count) -> None:
    gate.manual = False
    for _ in range(count):
        await asyncio.wait_for(ctx.heartbeat.tick(), TIMEOUT)


# ---------------------------------------------------------------------- #
# Seed data
# ---------------------------------------------------------------------- #
async def test_demo_office_has_four_agents_a_policy_and_no_secret(client):
    body = (await client.get("/api/office")).json()
    assert body["business"]["name"] == "Kantor Demo" and body["business"]["type"] == "demo"
    assert [a["name"] for a in body["agents"]] == ["Sari", "Budi", "Wati", "Joko"]
    assert [a["desk_index"] for a in body["agents"]] == [0, 1, 2, 3]
    assert body["policy"] == {"daily_limit_idr": DAILY_LIMIT_IDR, "blocked_actions": [], "is_set": True}
    assert body["demo_mode"] is True  # no API key: the fake LLM client
    assert not re.search(r"api[_-]?key|secret|token", json.dumps(body), re.I)


def test_demo_names_map_to_a_gender_in_the_front_end():
    """Sari and Wati read as women, Budi and Joko as men (char-look.js name lists)."""
    source = (Path(__file__).parent.parent / "skeleton/office/static/char-look.js").read_text()
    female = set(re.findall(r'"(\w+)"', re.search(r"FEMALE_NAMES = .*?\]\)", source, re.S).group(0)))
    male = set(re.findall(r'"(\w+)"', re.search(r"(?<![A-Z])MALE_NAMES = .*?\]\)", source, re.S).group(0)))
    assert {"sari", "wati"} <= female and {"budi", "joko"} <= male
    assert [name for name, _role, _plan in DEMO_STAFF] == ["Sari", "Budi", "Wati", "Joko"]


def test_demo_binds_to_localhost_only():
    assert HOST == "127.0.0.1"


def test_seed_demo_is_idempotent_and_keeps_the_owners_policy(tmp_path):
    path = str(tmp_path / "demo.db")
    first = build_demo(path, autostart=False)
    first.conn.execute("UPDATE policy SET daily_limit_idr = 42000")
    first_ids = dict(first.conn.execute("SELECT name, id FROM agent").fetchall())
    first.conn.close()

    conn = db.connect(path)
    business_id, ids = seed_demo(conn)
    assert business_id == first.business_id and ids == first_ids
    assert conn.execute("SELECT COUNT(*) FROM business").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == 4
    assert conn.execute("SELECT daily_limit_idr FROM policy").fetchone()[0] == 42000
    conn.close()


# ---------------------------------------------------------------------- #
# One full lap: working, handoff, waiting, idle, paused
# ---------------------------------------------------------------------- #
async def test_every_visual_state_is_produced_and_reported_consistently(ctx, client, gate, ids):
    observed: set[str] = set()
    sari, budi, wati, joko = ids["Sari"], ids["Budi"], ids["Wati"], ids["Joko"]
    assert await check_consistent(client, observed) == dict.fromkeys(ids.values(), "idle")

    tick = asyncio.create_task(ctx.heartbeat.tick())
    try:
        # Sari reads the sales notes: working, and only she (Req 2.5, 3.2).
        await gate.arrived()
        body = await wait_state(client, sari, "working")
        assert body["bubble_text"] == "Memeriksa catatan penjualan hari ini"
        states = await check_consistent(client, observed)
        assert states == {sari: "working", budi: "idle", wati: "idle", joko: "idle"}
        gate.release()

        # Budi hands a task to Wati: Budi shows a handoff to Wati (Req 2.6).
        await gate.arrived()
        body = await wait_state(client, budi, "handoff")
        snapshot = await sse_snapshot(client.app)
        assert snapshot[budi]["state"] == "handoff" and snapshot[budi]["target_agent_id"] == wati
        assert body["bubble_text"] == "Handoff to Wati"
        states = await check_consistent(client, observed)
        assert states[budi] == "handoff" and states[sari] == "idle"
        gate.release()

        # Wati works on her own task, then ends it as pending: she waits (Req 2.7).
        await gate.arrived()
        await wait_state(client, wati, "working")
        gate.release()

        # Joko spends Rp 5.000 inside the limit (Req 5.4).
        await gate.arrived()
        await wait_state(client, joko, "working")
        gate.release()
        results = await asyncio.wait_for(tick, TIMEOUT)
    except BaseException:
        tick.cancel()  # do not leave a task parked at the gate
        raise

    # Sari read, Budi's handed-over task ran (as Wati), Wati's own task ended pending, Joko spent.
    assert [r.status for r in results] == ["done", "done", "pending", "done"]
    waiting = await wait_state(client, wati, "waiting")
    assert waiting["bubble_text"] == "Menunggu OK pemilik"
    # Budi's handoff ended with his next state report: he is idle again.
    await settle_and_check(client, observed, {sari: "idle", budi: "idle", wati: "waiting", joko: "idle"})

    # Everything that happened is in the activity log with the right agents (Req 7.1).
    log = (await client.get("/api/activity?limit=100")).json()
    by_action = {(r["action"], r["agent_id"], r["status"]) for r in log}
    assert ("read", sari, "done") in by_action
    assert ("handoff", budi, "done") in by_action
    assert ("read", wati, "done") in by_action  # the handed-over task, attributed to Wati
    assert ("read", wati, "pending") in by_action  # Wati's own task, waiting for the owner
    assert ("spend", joko, "done") in by_action

    # Pause one agent: paused, and no new task starts for her (Req 8.1, 3.2).
    response = await client.post(f"/api/agents/{sari}/pause", json={"paused": True})
    assert response.json()["paused"] is True
    await wait_state(client, sari, "paused")
    states = await check_consistent(client, observed)
    assert states[sari] == "paused"

    # Pause the whole office: every agent paused, a tick starts nothing.
    before = len((await client.get("/api/activity?limit=500")).json())
    await client.post("/api/office/pause", json={"paused": True})
    await settle(client, dict.fromkeys(ids.values(), "paused"))
    await check_consistent(client, observed)
    gate.manual = True
    assert await asyncio.wait_for(ctx.heartbeat.tick(), TIMEOUT) == []
    assert gate.entered.empty()
    assert len((await client.get("/api/activity?limit=500")).json()) == before

    # Resume: back to the states the backend really has.
    await client.post("/api/office/pause", json={"paused": False})
    await client.post(f"/api/agents/{sari}/pause", json={"paused": False})
    await settle(client, {sari: "idle", budi: "idle", wati: "waiting", joko: "idle"})

    assert observed == {"working", "handoff", "waiting", "idle", "paused"}


async def settle_and_check(client, observed, expected) -> dict[int, str]:
    await settle(client, expected)
    states = await check_consistent(client, observed)
    assert states == expected
    return states


# ---------------------------------------------------------------------- #
# Several laps: waiting lasts, the limit holds, the outbox fills
# ---------------------------------------------------------------------- #
async def test_waiting_agent_stays_amber_until_her_next_task(ctx, client, gate, ids):
    wati = ids["Wati"]
    history = []
    gate.manual = False
    # Tick 1 ends pending, ticks 2 to 4 do nothing, tick 5 runs a task that finishes: idle.
    expected = ["waiting", "waiting", "waiting", "waiting", "idle", "idle"]
    for state in expected:
        await asyncio.wait_for(ctx.heartbeat.tick(), TIMEOUT)
        # The tracker applies the last events a moment after the tick returns.
        history.append((await wait_state(client, wati, state))["state"])
    assert history == expected


async def test_spend_never_exceeds_the_limit_and_rejections_are_logged(ctx, client, gate, ids):
    await tick_auto(ctx, gate, LAP_TICKS * 3)
    office = (await client.get("/api/office")).json()
    spent = office["spent_today_idr"]
    assert 0 < spent <= DAILY_LIMIT_IDR

    log = (await client.get("/api/activity?limit=500")).json()
    allowed = [r for r in log if r["action"] == "spend" and r["status"] == "done"]
    rejected = [r for r in log if r["action"] == "spend" and r["status"] == "rejected"]
    assert allowed and rejected
    assert all(r["reason"] == "daily spending limit reached" for r in rejected)
    assert all(r["cost_idr"] == 0 for r in rejected)  # a rejection never costs anything
    # The log, the ledger and the office endpoint agree to the rupiah.
    assert sum(r["cost_idr"] for r in allowed) == spent
    ledger = ctx.conn.execute("SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger").fetchone()[0]
    assert ledger == spent
    # No prefix of the ledger ever went over the limit.
    running = 0
    for (amount,) in ctx.conn.execute("SELECT amount_idr FROM spend_ledger ORDER BY id"):
        running += amount
        assert running <= DAILY_LIMIT_IDR
    # The agent panel shows the same number for the spender.
    joko = await agent_view(client, ids["Joko"])
    assert joko["spent_today_idr"] == spent
    assert joko["recent_actions"] and joko["recent_actions"][0]["action"] == "spend"


async def test_customer_message_waits_in_the_outbox_and_is_never_sent(ctx, client, gate, ids):
    await tick_auto(ctx, gate, LAP_TICKS)
    outbox = (await client.get("/api/outbox")).json()
    assert len(outbox) == 1
    message = outbox[0]
    assert message["status"] == "pending"
    assert message["agent_id"] == ids["Sari"]
    assert message["channel"] == "whatsapp" and message["recipient"] == "Pelanggan Demo"
    assert "tagihan" in message["body"].lower()  # text from the fake LLM, in Indonesian
    log = (await client.get("/api/activity?limit=500")).json()
    sent = [r for r in log if r["action"] == "customer_message"]
    assert [r["status"] for r in sent] == ["done"] and "outbox" in sent[0]["reason"]
    assert (await client.get("/api/summary")).json()["pending_outbox"] == 1
    # The draft task ran too, with the model text, and nothing else left the outbox.
    assert any(r["action"] == "draft" and r["status"] == "done" for r in log)


async def test_no_response_carries_a_secret(ctx, client, gate, ids):
    await tick_auto(ctx, gate, LAP_TICKS)
    assert ctx.settings.llm_api_key == ""
    paths = ["/api/office", "/api/activity?limit=500", "/api/outbox", "/api/summary"]
    paths += [f"/api/agents/{i}" for i in ids.values()]
    for path in paths:
        text = (await client.get(path)).text
        assert not re.search(r"api[_-]?key|authorization|bearer|secret", text, re.I), path
    assert "llm_api_key" not in repr(ctx)
