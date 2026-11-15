"""Kantor Warung as a whole app: first run, onboarding, owner controls, restart (Req 1.1 to 1.4, 10.1 to 10.3).

``create_warung_runtime`` builds the real wiring (database, policy engine, Manager, heartbeat, tracker,
API) on a database file in ``tmp_path``. The app is created with ``autostart=False`` and the test drives
``heartbeat.tick()`` by hand with a ``FakeClock``. The LLM is the offline fake (no API key), except in the
one test that proves a key never leaves the server; there the "model" is a refused loopback port.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.kantor_warung import app as warung_app
from apps.kantor_warung.app import create_warung_runtime
from apps.kantor_warung.roles import CashierRole
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.office import create_app
from skeleton.office.onboarding import AlreadyOnboarded
from skeleton.policy.engine import authorize
from skeleton.policy.engine import set_policy as engine_set_policy
from skeleton.runtime import Task, TaskKind

REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-FAKE-warung-key-do-not-leak-0123456789"
START = datetime(2025, 3, 10, 7, 0, tzinfo=JAKARTA)  # a Monday, 07:00 Jakarta time
NAME = "Warung Bu Tini"
API_PATHS_NEEDING_A_BUSINESS = [
    ("GET", "/api/agents/1", None),
    ("GET", "/api/activity", None),
    ("GET", "/api/summary", None),
    ("GET", "/api/outbox", None),
    ("GET", "/api/events", None),
    ("POST", "/api/office/pause", {"paused": True}),
    ("POST", "/api/agents/1/pause", {"paused": True}),
    ("PATCH", "/api/agents/1", {"name": "Sari2"}),
    ("PUT", "/api/policy", {"daily_limit_idr": 100000, "blocked_actions": []}),
    ("PUT", "/api/policy", {"garbage": True}),  # a bad body still gets the 409, not a 422
]


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "warung.db")


@pytest.fixture
def runtime(db_path, clock):
    rt = create_warung_runtime(db_path, settings=Settings(), clock=clock, autostart=False)
    yield rt
    if rt.ctx is not None:
        rt.ctx.conn.close()


@pytest.fixture
async def client(runtime):
    app = create_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://warung") as c:
        yield c


async def onboard(client, name=NAME, type_="warung"):
    return await client.post("/api/onboarding", json={"name": name, "type": type_})


async def set_policy(client, limit=100_000, blocked=()):
    return await client.put("/api/policy", json={"daily_limit_idr": limit, "blocked_actions": list(blocked)})


def count(db_path, table) -> int:
    """Row count read through a second connection, so it also works before the office exists."""
    other = sqlite3.connect(db_path)
    try:
        return other.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        other.close()


# ---------------------------------------------------------------------- #
# First run
# ---------------------------------------------------------------------- #
async def test_first_run_asks_for_onboarding(client, runtime):
    assert runtime.ctx is None
    body = (await client.get("/api/office")).json()
    assert body["onboarding_required"] is True
    [warung] = body["business_types"]
    assert warung["key"] == "warung" and warung["label"] and warung["description"]
    assert warung["agents"] == [
        {"name": "Sari", "role": "Kasir"},
        {"name": "Budi", "role": "Stok"},
        {"name": "Wati", "role": "Keuangan"},
        {"name": "Joko", "role": "Marketing"},
    ]
    assert set(body) == {"onboarding_required", "business_types"}


@pytest.mark.parametrize("method, path, body", API_PATHS_NEEDING_A_BUSINESS)
async def test_other_endpoints_answer_409_before_onboarding(client, runtime, method, path, body, db_path):
    response = await client.request(method, path, json=body)
    assert response.status_code == 409
    assert response.json() == {"error": "onboarding_required"}
    assert count(db_path, "business") == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"name": NAME, "type": "bengkel"},  # unknown type
        {"name": NAME, "type": ""},
        {"name": NAME, "type": 1},
        {"name": NAME, "type": None},
        {"name": NAME},
        {"type": "warung"},
        {"name": "", "type": "warung"},
        {"name": "   ", "type": "warung"},
        {"name": "x" * 61, "type": "warung"},
        {"name": "Bu\x00Tini", "type": "warung"},
        {"name": 12, "type": "warung"},
        {"name": NAME, "type": "warung", "extra": 1},
        [],
    ],
)
async def test_bad_onboarding_is_422_and_writes_nothing(client, runtime, payload, db_path):
    response = await client.post("/api/onboarding", json=payload)
    assert response.status_code == 422
    assert runtime.ctx is None
    assert all(count(db_path, t) == 0 for t in ("business", "agent", "product", "sale"))
    assert (await client.get("/api/office")).json()["onboarding_required"] is True  # still first run


async def test_a_422_does_not_echo_the_input(client):
    secret = "SECRET-TYPE-VALUE"
    response = await client.post("/api/onboarding", json={"name": NAME, "type": secret})
    assert response.status_code == 422 and secret not in response.text


async def test_not_json_is_422(client):
    response = await client.post("/api/onboarding", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 422


async def test_onboarding_creates_the_office(client, runtime, db_path):
    response = await onboard(client, "  Warung Bu Tini  ")
    assert response.status_code == 200
    body = response.json()
    assert body["business"]["name"] == NAME and body["business"]["type"] == "warung"
    assert body["business"]["paused"] is False
    assert [(a["name"], a["role"], a["desk_index"]) for a in body["agents"]] == [
        ("Sari", "Kasir", 0), ("Budi", "Stok", 1), ("Wati", "Keuangan", 2), ("Joko", "Marketing", 3),
    ]
    assert body["policy"] == {"daily_limit_idr": None, "blocked_actions": [], "is_set": False}  # Req 1.3
    assert body["spent_today_idr"] == 0 and body["demo_mode"] is True
    assert (await client.get("/api/office")).json() == body  # the normal payload, from now on
    assert runtime.ctx is not None and runtime.ctx.autostart is False
    assert count(db_path, "product") == 12 and count(db_path, "sale") > 50  # demo data (Req 10.1)


async def test_second_onboarding_is_409_and_changes_nothing(client, runtime, db_path):
    first = (await onboard(client)).json()
    before = (count(db_path, "business"), count(db_path, "agent"), count(db_path, "product"), count(db_path, "sale"))
    again = await onboard(client, "Another Shop")
    assert again.status_code == 409 and again.json() == {"error": "already_onboarded"}
    assert (await client.get("/api/office")).json() == first
    assert before == (count(db_path, "business"), count(db_path, "agent"), count(db_path, "product"), count(db_path, "sale"))


async def test_endpoints_work_after_onboarding(client):
    await onboard(client)
    agents = (await client.get("/api/office")).json()["agents"]
    detail = await client.get(f"/api/agents/{agents[0]['id']}")
    assert detail.status_code == 200 and detail.json()["role"] == "Kasir"
    assert (await client.get("/api/activity")).status_code == 200
    summary = (await client.get("/api/summary")).json()
    assert summary["pending_outbox"] == 0
    assert (await client.get("/api/outbox")).json() == []
    assert (await client.get("/api/agents/9999")).status_code == 404  # a real 404 now, not a 409


# ---------------------------------------------------------------------- #
# Policy (Req 1.3) and the agents at work
# ---------------------------------------------------------------------- #
async def test_agents_cannot_spend_until_the_policy_is_set_then_they_can(client, runtime, db_path):
    await onboard(client)
    ctx = runtime.ctx
    stock_id = ctx.conn.execute("SELECT id FROM agent WHERE role = 'stock'").fetchone()[0]
    spend = Task(agent_id=stock_id, kind=TaskKind.SPEND, payload={}, cost_idr=10_000, reason="Beli gula")

    office = (await client.get("/api/office")).json()
    assert office["policy"]["is_set"] is False and office["spent_today_idr"] == 0
    decision = authorize(ctx.conn, ctx.business_id, spend, ctx.clock)
    assert not decision.allowed and decision.reason == "policy not set"
    assert count(db_path, "spend_ledger") == 0

    put = await set_policy(client, 100_000, [])
    assert put.status_code == 200 and put.json() == {"daily_limit_idr": 100_000, "blocked_actions": [], "is_set": True}
    await ctx.heartbeat.tick()  # the stock agent now buys what fits the limit
    office = (await client.get("/api/office")).json()
    assert 0 < office["spent_today_idr"] <= 100_000
    assert count(db_path, "spend_ledger") >= 1
    assert count(db_path, "purchase_order") >= 1


async def test_a_blocked_purchase_never_spends(client, runtime, db_path):
    await onboard(client)
    await set_policy(client, 100_000, ["spend"])
    await runtime.ctx.heartbeat.tick()
    assert count(db_path, "spend_ledger") == 0
    assert (await client.get("/api/office")).json()["spent_today_idr"] == 0


async def test_a_two_day_run_produces_activity_from_all_four_roles(client, runtime):
    await onboard(client)
    await set_policy(client, 100_000, [])
    ids = {a["role"]: a["id"] for a in (await client.get("/api/office")).json()["agents"]}
    clock = runtime.ctx.clock
    for _ in range(2 * 24):  # hourly ticks for two days, from 07:00 on day 1
        await runtime.ctx.heartbeat.tick()
        clock.advance(3600)

    rows = (await client.get("/api/activity?limit=500")).json()
    by_agent = {}
    for row in rows:
        by_agent.setdefault(row["agent_id"], []).append(row)
    for role in ("Kasir", "Stok", "Keuangan", "Marketing"):
        assert by_agent.get(ids[role]), f"no activity from the {role} agent"
    # The marketing agent reacted to the declining sales in the demo data, and nothing was sent.
    assert any("penjualan turun" in r["reason"].lower() for r in by_agent[ids["Marketing"]])
    outbox = (await client.get("/api/outbox")).json()
    assert outbox and all(m["status"] == "pending" for m in outbox)
    spent = (await client.get("/api/office")).json()["spent_today_idr"]
    assert 0 <= spent <= 100_000


# ---------------------------------------------------------------------- #
# Rename (Req 1.4)
# ---------------------------------------------------------------------- #
async def test_rename_persists_and_shows_in_the_office(client, runtime, db_path):
    await onboard(client)
    sari = (await client.get("/api/office")).json()["agents"][0]
    response = await client.patch(f"/api/agents/{sari['id']}", json={"name": "  Sarita ", "role": "Kasir Utama"})
    assert response.status_code == 200
    assert response.json()["name"] == "Sarita" and response.json()["role"] == "Kasir Utama"
    agents = (await client.get("/api/office")).json()["agents"]
    assert (agents[0]["name"], agents[0]["role"]) == ("Sarita", "Kasir Utama")
    assert (await client.get(f"/api/agents/{sari['id']}")).json()["name"] == "Sarita"
    # The stable role key did not change, so the cashier still works.
    key = runtime.ctx.conn.execute("SELECT role FROM agent WHERE id = ?", (sari["id"],)).fetchone()[0]
    assert key == "cashier"

    runtime.ctx.conn.close()
    again = create_warung_runtime(db_path, settings=Settings(), clock=FakeClock(START), autostart=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(again)), base_url="http://w") as c2:
            restarted = (await c2.get("/api/office")).json()["agents"]
        assert (restarted[0]["name"], restarted[0]["role"]) == ("Sarita", "Kasir Utama")
        assert isinstance(again.ctx.heartbeat._roles[sari["id"]], CashierRole)  # the role key survived the rename
    finally:
        again.ctx.conn.close()


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": "x" * 81}, {"name": 5}, {"colour": "red"}])
async def test_bad_rename_is_422_and_changes_nothing(client, body):
    await onboard(client)
    before = (await client.get("/api/office")).json()
    response = await client.patch("/api/agents/1", json=body)
    assert response.status_code == 422
    assert (await client.get("/api/office")).json() == before


# ---------------------------------------------------------------------- #
# Restart on the same database file
# ---------------------------------------------------------------------- #
async def test_restart_on_the_same_database_skips_onboarding(client, runtime, db_path, clock):
    await onboard(client)
    await set_policy(client, 50_000, ["customer_message"])
    first = (await client.get("/api/office")).json()
    runtime.ctx.conn.close()

    again = create_warung_runtime(db_path, settings=Settings(), clock=clock, autostart=False)
    try:
        assert again.ctx is not None  # straight into the office
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(again)), base_url="http://w") as c2:
            office = (await c2.get("/api/office")).json()
            assert "onboarding_required" not in office
            assert office == first  # same business, agents, policy
            assert (await c2.post("/api/onboarding", json={"name": "Other", "type": "warung"})).status_code == 409
            # roles were rebuilt: the heartbeat gets work done after the restart
            results = await again.ctx.heartbeat.tick()
            assert results, "no agent did anything after the restart"
    finally:
        again.ctx.conn.close()


def test_a_database_of_another_business_type_is_refused(db_path):
    from skeleton.core import db

    conn = db.connect(db_path)
    db.init_schema(conn)
    conn.execute("INSERT INTO business (name, type) VALUES ('Salon', 'jasa')")
    conn.close()
    with pytest.raises(ValueError, match="jasa"):
        create_warung_runtime(db_path, settings=Settings(), autostart=False)


def test_the_callback_refuses_a_second_business_directly(runtime):
    runtime.bootstrap_callback("Satu", "warung")
    with pytest.raises(AlreadyOnboarded):
        runtime.bootstrap_callback("Dua", "warung")


# ---------------------------------------------------------------------- #
# The heartbeat waits for the policy when the app runs for real (Req 1.3)
# ---------------------------------------------------------------------- #
def test_with_autostart_the_heartbeat_starts_when_the_policy_is_saved(db_path, clock):
    runtime = create_warung_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=True)
    with TestClient(create_app(runtime)) as http:
        assert http.get("/api/office").json()["onboarding_required"] is True
        assert http.post("/api/onboarding", json={"name": NAME, "type": "warung"}).status_code == 200
        ctx = runtime.ctx
        assert ctx.heartbeat._task is None, "agents must not start before the owner sets the policy"
        assert http.get("/api/office").json()["policy"]["is_set"] is False
        assert http.put("/api/policy", json={"daily_limit_idr": 100000, "blocked_actions": []}).status_code == 200
        assert ctx.heartbeat._task is not None and not ctx.heartbeat._task.done()
    assert ctx.heartbeat._task is None  # stopped with the app
    ctx.conn.close()


def test_with_autostart_a_restart_with_a_policy_starts_the_heartbeat_at_once(db_path, clock):
    first = create_warung_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=False)
    first_ctx = first.bootstrap_callback(NAME, "warung")
    engine_set_policy(first_ctx.conn, first_ctx.business_id, 100_000, [])
    first_ctx.conn.close()
    runtime = create_warung_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=True)
    with TestClient(create_app(runtime)):
        assert runtime.ctx.heartbeat._task is not None
    runtime.ctx.conn.close()


# ---------------------------------------------------------------------- #
# The API key never leaves the server (Req 10.2)
# ---------------------------------------------------------------------- #
async def test_the_api_key_never_appears_in_any_response(db_path, clock):
    settings = Settings(llm_api_key=FAKE_KEY, llm_base_url="http://127.0.0.1:9", model_low="m", model_strong="m")
    runtime = create_warung_runtime(db_path, settings=settings, clock=clock, autostart=False)
    seen: list[str] = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://w") as c:
            async def get(path):
                response = await c.get(path)
                seen.append(response.text)
                return response

            async def send(method, path, body):
                response = await c.request(method, path, json=body)
                seen.append(response.text)
                return response

            await get("/api/office")  # first run
            await send("POST", "/api/onboarding", {"name": NAME, "type": "bengkel"})  # 422
            await send("POST", "/api/onboarding", {"name": NAME, "type": "warung"})
            await send("POST", "/api/onboarding", {"name": NAME, "type": "warung"})  # 409
            await send("PUT", "/api/policy", {"daily_limit_idr": 100_000, "blocked_actions": []})
            await runtime.ctx.heartbeat.tick()  # the model is unreachable: the failure goes to the log
            office = (await get("/api/office")).json()
            assert office["demo_mode"] is False
            for agent in office["agents"]:
                await get(f"/api/agents/{agent['id']}")
            await get("/api/activity?limit=500")
            await get("/api/summary")
            await get("/api/outbox")
            await send("PATCH", f"/api/agents/{office['agents'][0]['id']}", {"name": "Sari2"})
        assert seen and all(FAKE_KEY not in text for text in seen)
        assert FAKE_KEY not in repr(runtime.ctx) and FAKE_KEY not in repr(runtime.ctx.settings)
        assert FAKE_KEY not in str(runtime.ctx.settings.public_dict())
    finally:
        runtime.ctx.conn.close()


# ---------------------------------------------------------------------- #
# Command line and run entry (Req 10.3)
# ---------------------------------------------------------------------- #
def test_command_line_defaults():
    args = warung_app._parse_args([])
    assert (args.port, args.db, args.host, args.interval) == (8000, "kantor-warung.db", "127.0.0.1", None)
    args = warung_app._parse_args(["--port", "9001", "--db", "x.db", "--interval", "10", "--host", "localhost"])
    assert (args.port, args.db, args.host, args.interval) == (9001, "x.db", "localhost", 10.0)
    assert warung_app.DEFAULT_HOST != "0.0.0.0"


def test_main_serves_on_loopback_by_default(monkeypatch, tmp_path):
    served = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(app=app, **kw))
    warung_app.main([])
    assert served["host"] == "127.0.0.1" and served["port"] == 8000
    assert (tmp_path / "kantor-warung.db").exists()
    assert served["app"].state.office is None  # first run: no business yet


@pytest.mark.parametrize("argv", [["--port", "0"], ["--port", "70000"], ["--interval", "0"], ["--interval", "0.5"]])
def test_main_rejects_bad_numbers(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        warung_app.main(argv)
    assert not (tmp_path / "kantor-warung.db").exists()


def test_main_refuses_another_business_type_with_a_clear_message(tmp_path):
    from skeleton.core import db

    path = str(tmp_path / "salon.db")
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO business (name, type) VALUES ('Salon', 'jasa')")
    conn.close()
    with pytest.raises(SystemExit, match="Cannot start Kantor Warung"):
        warung_app.main(["--db", path])


def test_the_run_entry_works_from_any_folder(tmp_path):
    entry = REPO / "apps" / "kantor-warung" / "app.py"
    result = subprocess.run([sys.executable, str(entry), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for option in ("--port", "--db", "--interval", "--host", "--real-hours", "--no-pacing", "--pace-min", "--pace-max"):
        assert option in result.stdout


def test_readme_documents_the_run_command():
    readme = (REPO / "apps" / "kantor-warung" / "README.md").read_text(encoding="utf-8")
    assert ".venv/bin/python apps/kantor-warung/app.py --port 8000" in readme
    assert "127.0.0.1" in readme


def test_seed_writes_only_through_the_data_layer():
    source = (REPO / "apps" / "kantor_warung" / "seed.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", code, re.I), "seed.py must not write money tables with raw SQL"
    assert "data.record_sale" in code and "data.add_product" in code


# ---------------------------------------------------------------------- #
# Demo hours (task 14.1): lively at any hour, unless --real-hours
# ---------------------------------------------------------------------- #
NIGHT = datetime(2025, 3, 10, 2, 0, tzinfo=JAKARTA)  # 02:00 on a Monday


def _actions(runtime):
    return [r[0] for r in runtime.ctx.conn.execute("SELECT action FROM activity ORDER BY id")]


async def _night_ticks(runtime, client, ticks=30):
    assert (await onboard(client)).status_code == 200
    assert (await set_policy(client)).status_code == 200
    for _ in range(ticks):
        await runtime.ctx.heartbeat.tick()
        runtime.ctx.clock.advance(8)


async def test_the_staff_sell_at_night_by_default(db_path):
    runtime = create_warung_runtime(db_path, settings=Settings(), clock=FakeClock(NIGHT), interval=10, autostart=False)
    try:
        app = create_app(runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://warung") as c:
            await _night_ticks(runtime, c, ticks=80)
        assert "Catat penjualan" in _actions(runtime)  # demo hours: a sale at 02:00 without any clock trick
        feed = next(r for r in runtime.ctx.heartbeat._roles.values() if isinstance(r, CashierRole)).feed
        assert feed.demo_hours is True and feed.interval_seconds == 10
    finally:
        runtime.ctx.conn.close()


async def test_real_hours_keep_the_shop_closed_at_night(db_path):
    runtime = create_warung_runtime(
        db_path, settings=Settings(), clock=FakeClock(NIGHT), interval=10, autostart=False, real_hours=True
    )
    try:
        app = create_app(runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://warung") as c:
            await _night_ticks(runtime, c, ticks=80)
        assert "Catat penjualan" not in _actions(runtime)
        feed = next(r for r in runtime.ctx.heartbeat._roles.values() if isinstance(r, CashierRole)).feed
        assert feed.demo_hours is False
    finally:
        runtime.ctx.conn.close()


def test_real_hours_flag_and_environment_reach_the_runtime(monkeypatch, tmp_path):
    seen = []
    monkeypatch.chdir(tmp_path)
    real = warung_app.create_warung_runtime

    def spy(db_path, settings=None, **kw):
        seen.append(kw.get("real_hours"))
        return real(db_path, settings=settings, **kw)

    monkeypatch.setattr(warung_app, "create_warung_runtime", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.delenv("KANTOR_REAL_HOURS", raising=False)
    warung_app.main([])
    warung_app.main(["--real-hours"])
    monkeypatch.setenv("KANTOR_REAL_HOURS", "1")
    warung_app.main([])
    monkeypatch.setenv("KANTOR_REAL_HOURS", "0")
    warung_app.main([])
    assert seen == [False, True, True, False]
    assert warung_app._parse_args([]).real_hours is False and warung_app._parse_args(["--real-hours"]).real_hours is True


def test_a_bad_real_hours_environment_value_stops_the_app_with_a_message(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KANTOR_REAL_HOURS", "maybe")
    with pytest.raises(SystemExit, match="KANTOR_REAL_HOURS"):
        warung_app.main([])


def test_readme_documents_hours_and_the_pacing_flags():
    readme = (REPO / "apps" / "kantor-warung" / "README.md").read_text(encoding="utf-8")
    for flag in ("--no-pacing", "--pace-min", "--pace-max", "--real-hours", "KANTOR_REAL_HOURS", "KANTOR_PACING_MIN"):
        assert flag in readme, flag
    assert "docs/pacing.md" in readme
