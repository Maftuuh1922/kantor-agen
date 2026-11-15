"""Kantor Jasa as a whole app: first run, onboarding, owner controls, restart (Req 1.1, 1.2, 9.2, 10.1 to 10.3).

``create_jasa_runtime`` builds the real wiring (database, policy engine, Manager, heartbeat, tracker,
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

from apps.kantor_jasa import app as jasa_app
from apps.kantor_jasa.app import create_jasa_runtime
from apps.kantor_jasa.roles import BillingRole, BookingRole, FollowUpRole
from apps.kantor_jasa.template import JASA_TEMPLATE
from apps.kantor_warung.template import WARUNG_TEMPLATE
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.office import create_app
from skeleton.office.onboarding import AlreadyOnboarded, TemplateRegistry
from skeleton.policy.engine import authorize
from skeleton.policy.engine import set_policy as engine_set_policy
from skeleton.runtime import Task, TaskKind

REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-FAKE-jasa-key-do-not-leak-0123456789"
START = datetime(2025, 3, 10, 7, 0, tzinfo=JAKARTA)  # a Monday, 07:00 Jakarta time
NAME = "Salon Mawar"
API_PATHS_NEEDING_A_BUSINESS = [
    ("GET", "/api/agents/1", None),
    ("GET", "/api/activity", None),
    ("GET", "/api/summary", None),
    ("GET", "/api/outbox", None),
    ("GET", "/api/events", None),
    ("POST", "/api/office/pause", {"paused": True}),
    ("POST", "/api/agents/1/pause", {"paused": True}),
    ("PATCH", "/api/agents/1", {"name": "Dewi2"}),
    ("PUT", "/api/policy", {"daily_limit_idr": 100000, "blocked_actions": []}),
    ("PUT", "/api/policy", {"garbage": True}),  # a bad body still gets the 409, not a 422
]


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "jasa.db")


@pytest.fixture
def runtime(db_path, clock):
    rt = create_jasa_runtime(db_path, settings=Settings(), clock=clock, autostart=False)
    yield rt
    if rt.ctx is not None:
        rt.ctx.conn.close()


@pytest.fixture
async def client(runtime):
    app = create_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://jasa") as c:
        yield c


async def onboard(client, name=NAME, type_="jasa"):
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


def has_table(db_path, table) -> bool:
    other = sqlite3.connect(db_path)
    try:
        return other.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None
    finally:
        other.close()


# ---------------------------------------------------------------------- #
# First run
# ---------------------------------------------------------------------- #
async def test_first_run_asks_for_onboarding_and_offers_only_jasa(client, runtime):
    assert runtime.ctx is None
    body = (await client.get("/api/office")).json()
    assert body["onboarding_required"] is True
    [jasa] = body["business_types"]  # one type only: the Warung type is not offered here
    assert jasa["key"] == "jasa" and jasa["label"] == "Kantor Jasa"
    assert all(word in jasa["description"].lower() for word in ("salon", "bengkel", "bimbel", "servis ac"))
    assert jasa["agents"] == [
        {"name": "Dewi", "role": "Booking"},
        {"name": "Agus", "role": "Tagihan"},
        {"name": "Rina", "role": "Tindak lanjut"},
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
        {"name": NAME, "type": "warung"},  # a real type, but not offered by this app
        {"name": NAME, "type": "bengkel"},  # unknown type
        {"name": NAME, "type": ""},
        {"name": NAME, "type": 1},
        {"name": NAME, "type": None},
        {"name": NAME},
        {"type": "jasa"},
        {"name": "", "type": "jasa"},
        {"name": "   ", "type": "jasa"},
        {"name": "x" * 61, "type": "jasa"},
        {"name": "Sa\x00lon", "type": "jasa"},
        {"name": 12, "type": "jasa"},
        {"name": NAME, "type": "jasa", "extra": 1},
        [],
    ],
)
async def test_bad_onboarding_is_422_and_writes_nothing(client, runtime, payload, db_path):
    response = await client.post("/api/onboarding", json=payload)
    assert response.status_code == 422
    assert runtime.ctx is None
    assert all(count(db_path, t) == 0 for t in ("business", "agent"))
    assert not has_table(db_path, "customer")  # the Jasa tables are created by the seed, so not yet
    assert (await client.get("/api/office")).json()["onboarding_required"] is True  # still first run


async def test_a_422_does_not_echo_the_input(client):
    secret = "SECRET-TYPE-VALUE"
    response = await client.post("/api/onboarding", json={"name": NAME, "type": secret})
    assert response.status_code == 422 and secret not in response.text


async def test_not_json_is_422(client):
    response = await client.post("/api/onboarding", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 422


async def test_onboarding_creates_the_office(client, runtime, db_path):
    response = await onboard(client, "  Salon Mawar  ")
    assert response.status_code == 200
    body = response.json()
    assert body["business"]["name"] == NAME and body["business"]["type"] == "jasa"
    assert body["business"]["paused"] is False
    assert [(a["name"], a["role"], a["desk_index"]) for a in body["agents"]] == [
        ("Dewi", "Booking", 0), ("Agus", "Tagihan", 1), ("Rina", "Tindak lanjut", 2),
    ]
    assert body["policy"] == {"daily_limit_idr": None, "blocked_actions": [], "is_set": False}  # Req 1.3
    assert body["spent_today_idr"] == 0 and body["demo_mode"] is True
    assert (await client.get("/api/office")).json() == body  # the normal payload, from now on
    assert runtime.ctx is not None and runtime.ctx.autostart is False
    assert count(db_path, "customer") == 8 and count(db_path, "booking") >= 10  # demo data (Req 10.1)
    assert count(db_path, "invoice") >= 8
    # The agents carry the role keys that build_roles maps (Req 1.2).
    keys = [r[0] for r in runtime.ctx.conn.execute("SELECT role FROM agent ORDER BY desk_index")]
    assert keys == ["booking", "billing", "followup"]
    assert set(runtime.ctx.heartbeat._roles) == {1, 2, 3}


async def test_second_onboarding_is_409_and_changes_nothing(client, runtime, db_path):
    first = (await onboard(client)).json()
    tables = ("business", "agent", "customer", "booking", "invoice")
    before = [count(db_path, t) for t in tables]
    again = await onboard(client, "Another Salon")
    assert again.status_code == 409 and again.json() == {"error": "already_onboarded"}
    assert (await client.get("/api/office")).json() == first
    assert before == [count(db_path, t) for t in tables]


async def test_endpoints_work_after_onboarding(client):
    await onboard(client)
    agents = (await client.get("/api/office")).json()["agents"]
    detail = await client.get(f"/api/agents/{agents[0]['id']}")
    assert detail.status_code == 200 and detail.json()["role"] == "Booking"
    assert (await client.get("/api/activity")).status_code == 200
    summary = (await client.get("/api/summary")).json()
    assert summary["pending_outbox"] == 0
    assert (await client.get("/api/outbox")).json() == []
    assert (await client.get("/api/agents/9999")).status_code == 404  # a real 404 now, not a 409


# ---------------------------------------------------------------------- #
# Policy (Req 1.3) and the agents at work
# ---------------------------------------------------------------------- #
async def test_agents_cannot_spend_until_the_policy_is_set(client, runtime, db_path):
    await onboard(client)
    ctx = runtime.ctx
    billing_id = ctx.conn.execute("SELECT id FROM agent WHERE role = 'billing'").fetchone()[0]
    spend = Task(agent_id=billing_id, kind=TaskKind.SPEND, payload={}, cost_idr=10_000, reason="Beli pewangi")
    assert (await client.get("/api/office")).json()["policy"]["is_set"] is False
    decision = authorize(ctx.conn, ctx.business_id, spend, ctx.clock)
    assert not decision.allowed and decision.reason == "policy not set"
    assert count(db_path, "spend_ledger") == 0

    put = await set_policy(client, 100_000, [])
    assert put.status_code == 200 and put.json() == {"daily_limit_idr": 100_000, "blocked_actions": [], "is_set": True}
    assert authorize(ctx.conn, ctx.business_id, spend, ctx.clock).allowed


async def test_the_first_tick_after_the_policy_reacts_to_the_demo_data(client, runtime, db_path):
    await onboard(client)
    await set_policy(client, 100_000, [])
    results = await runtime.ctx.heartbeat.tick()
    messages = [r.message for r in results]
    assert any("menunggu pemilik" in m for m in messages), messages  # the big invoice waits for the owner (amber)
    assert any(r.status == "pending" for r in results)
    assert any("dikonfirmasi" in m for m in messages)
    reasons = [row["reason"] for row in (await client.get("/api/activity?limit=100")).json()]
    assert sum("Pengingat bayar" in r for r in reasons) == 2  # the two invoices unpaid for more than a week
    assert count(db_path, "outbox") >= 3


async def test_blocking_customer_messages_keeps_the_outbox_empty(client, runtime, db_path):
    await onboard(client)
    await set_policy(client, 100_000, ["customer_message"])
    await runtime.ctx.heartbeat.tick()
    assert count(db_path, "outbox") == 0
    assert (await client.get("/api/summary")).json()["pending_outbox"] == 0


async def test_a_two_day_run_produces_activity_from_all_three_roles(client, runtime):
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
    for role in ("Booking", "Tagihan", "Tindak lanjut"):
        assert by_agent.get(ids[role]), f"no activity from the {role} agent"
    # The booking agent confirmed tomorrow's requests, billing drafted the invoice of yesterday's service,
    # and follow-up chased the invoices that have been unpaid for more than a week.
    assert any("Konfirmasi booking" in r["reason"] for r in by_agent[ids["Booking"]])
    assert any("Buat invoice" in r["reason"] for r in by_agent[ids["Tagihan"]])
    assert any("Pengingat bayar" in r["reason"] for r in by_agent[ids["Tindak lanjut"]])
    # A message reaches the outbox as pending. Nothing is ever sent (Req 5.4).
    outbox = (await client.get("/api/outbox")).json()
    assert outbox and all(m["status"] == "pending" and m["channel"] == "whatsapp" for m in outbox)
    assert any("Rp 120.000" in m["body"] and "invoice #1" in m["body"] for m in outbox)  # the exact reminder
    assert (await client.get("/api/summary")).json()["pending_outbox"] == len(outbox)
    # Jasa spends no money: nothing is ever booked on the spend ledger.
    assert (await client.get("/api/office")).json()["spent_today_idr"] == 0


async def test_invoices_stay_immutable_after_a_run(client, runtime):
    await onboard(client)
    await set_policy(client, 100_000, [])
    for _ in range(24):
        await runtime.ctx.heartbeat.tick()
        runtime.ctx.clock.advance(3600)
    conn = runtime.ctx.conn
    approved = conn.execute("SELECT id FROM invoice WHERE status IN ('approved', 'paid') LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE invoice SET amount_idr = amount_idr + 1 WHERE id = ?", (approved,))


# ---------------------------------------------------------------------- #
# Rename (Req 1.4)
# ---------------------------------------------------------------------- #
async def test_rename_persists_and_shows_in_the_office(client, runtime, db_path):
    await onboard(client)
    dewi = (await client.get("/api/office")).json()["agents"][0]
    response = await client.patch(f"/api/agents/{dewi['id']}", json={"name": "  Dewina ", "role": "Resepsionis"})
    assert response.status_code == 200
    assert response.json()["name"] == "Dewina" and response.json()["role"] == "Resepsionis"
    agents = (await client.get("/api/office")).json()["agents"]
    assert (agents[0]["name"], agents[0]["role"]) == ("Dewina", "Resepsionis")
    assert (await client.get(f"/api/agents/{dewi['id']}")).json()["name"] == "Dewina"
    # The stable role key did not change, so the booking agent still works.
    key = runtime.ctx.conn.execute("SELECT role FROM agent WHERE id = ?", (dewi["id"],)).fetchone()[0]
    assert key == "booking"

    runtime.ctx.conn.close()
    again = create_jasa_runtime(db_path, settings=Settings(), clock=FakeClock(START), autostart=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(again)), base_url="http://j") as c2:
            restarted = (await c2.get("/api/office")).json()["agents"]
        assert (restarted[0]["name"], restarted[0]["role"]) == ("Dewina", "Resepsionis")
        roles = again.ctx.heartbeat._roles
        assert isinstance(roles[dewi["id"]], BookingRole)  # the role key survived the rename
        assert [type(r) for r in roles.values()] == [BookingRole, BillingRole, FollowUpRole]
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
    await set_policy(client, 50_000, ["spend"])
    first = (await client.get("/api/office")).json()
    runtime.ctx.conn.close()

    again = create_jasa_runtime(db_path, settings=Settings(), clock=clock, autostart=False)
    try:
        assert again.ctx is not None  # straight into the office
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(again)), base_url="http://j") as c2:
            office = (await c2.get("/api/office")).json()
            assert "onboarding_required" not in office
            assert office == first  # same business, agents, policy
            assert (await c2.post("/api/onboarding", json={"name": "Other", "type": "jasa"})).status_code == 409
            # roles were rebuilt, feeds included: the heartbeat gets work done after the restart
            results = await again.ctx.heartbeat.tick()
            assert results, "no agent did anything after the restart"
    finally:
        again.ctx.conn.close()


def test_a_database_of_another_business_type_is_refused(db_path):
    from skeleton.core import db

    conn = db.connect(db_path)
    db.init_schema(conn)
    conn.execute("INSERT INTO business (name, type) VALUES ('Warung', 'warung')")
    conn.close()
    with pytest.raises(ValueError, match="warung"):
        create_jasa_runtime(db_path, settings=Settings(), autostart=False)


def test_the_callback_refuses_a_second_business_directly(runtime):
    runtime.bootstrap_callback("Satu", "jasa")
    with pytest.raises(AlreadyOnboarded):
        runtime.bootstrap_callback("Dua", "jasa")


# ---------------------------------------------------------------------- #
# Two apps, one skeleton (Req 9.2)
# ---------------------------------------------------------------------- #
def test_both_templates_can_live_in_one_registry_and_each_app_offers_only_its_own(tmp_path):
    both = TemplateRegistry([WARUNG_TEMPLATE, JASA_TEMPLATE])
    assert both.keys() == ["warung", "jasa"]
    assert both.get("jasa") is JASA_TEMPLATE and both.get("warung") is WARUNG_TEMPLATE

    from apps.kantor_warung.app import create_warung_runtime

    jasa = create_jasa_runtime(str(tmp_path / "j.db"), settings=Settings(), autostart=False)
    warung = create_warung_runtime(str(tmp_path / "w.db"), settings=Settings(), autostart=False)
    try:
        assert jasa.templates.keys() == ["jasa"]
        assert warung.templates.keys() == ["warung"]
    finally:
        for rt in (jasa, warung):
            if rt.ctx is not None:
                rt.ctx.conn.close()


def test_the_skeleton_still_knows_no_business_type():
    for path in (REPO / "skeleton").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "kantor_jasa" not in text and "kantor_warung" not in text, path


# ---------------------------------------------------------------------- #
# The heartbeat waits for the policy when the app runs for real (Req 1.3)
# ---------------------------------------------------------------------- #
def test_with_autostart_the_heartbeat_starts_when_the_policy_is_saved(db_path, clock):
    runtime = create_jasa_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=True)
    with TestClient(create_app(runtime)) as http:
        assert http.get("/api/office").json()["onboarding_required"] is True
        assert http.post("/api/onboarding", json={"name": NAME, "type": "jasa"}).status_code == 200
        ctx = runtime.ctx
        assert ctx.heartbeat._task is None, "agents must not start before the owner sets the policy"
        assert http.get("/api/office").json()["policy"]["is_set"] is False
        assert http.put("/api/policy", json={"daily_limit_idr": 100000, "blocked_actions": []}).status_code == 200
        assert ctx.heartbeat._task is not None and not ctx.heartbeat._task.done()
    assert ctx.heartbeat._task is None  # stopped with the app
    ctx.conn.close()


def test_with_autostart_a_restart_with_a_policy_starts_the_heartbeat_at_once(db_path, clock):
    first = create_jasa_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=False)
    first_ctx = first.bootstrap_callback(NAME, "jasa")
    engine_set_policy(first_ctx.conn, first_ctx.business_id, 100_000, [])
    first_ctx.conn.close()
    runtime = create_jasa_runtime(db_path, settings=Settings(), clock=clock, interval=3600, autostart=True)
    with TestClient(create_app(runtime)):
        assert runtime.ctx.heartbeat._task is not None
    runtime.ctx.conn.close()


# ---------------------------------------------------------------------- #
# The API key never leaves the server (Req 10.2)
# ---------------------------------------------------------------------- #
async def test_the_api_key_never_appears_in_any_response(db_path, clock):
    settings = Settings(llm_api_key=FAKE_KEY, llm_base_url="http://127.0.0.1:9", model_low="m", model_strong="m")
    runtime = create_jasa_runtime(db_path, settings=settings, clock=clock, autostart=False)
    seen: list[str] = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://j") as c:
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
            await send("POST", "/api/onboarding", {"name": NAME, "type": "jasa"})
            await send("POST", "/api/onboarding", {"name": NAME, "type": "jasa"})  # 409
            await send("PUT", "/api/policy", {"daily_limit_idr": 100_000, "blocked_actions": []})
            await runtime.ctx.heartbeat.tick()  # the model is unreachable: the failure goes to the log
            office = (await get("/api/office")).json()
            assert office["demo_mode"] is False
            for agent in office["agents"]:
                await get(f"/api/agents/{agent['id']}")
            await get("/api/activity?limit=500")
            await get("/api/summary")
            await get("/api/outbox")
            await send("PATCH", f"/api/agents/{office['agents'][0]['id']}", {"name": "Dewi2"})
        assert seen and all(FAKE_KEY not in text for text in seen)
        assert FAKE_KEY not in repr(runtime.ctx) and FAKE_KEY not in repr(runtime.ctx.settings)
        assert FAKE_KEY not in str(runtime.ctx.settings.public_dict())
        # With the model down the messages are still queued (rule-based text), and the key stays out.
        assert runtime.ctx.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] >= 3
    finally:
        runtime.ctx.conn.close()


# ---------------------------------------------------------------------- #
# Command line and run entry (Req 10.3)
# ---------------------------------------------------------------------- #
def test_command_line_defaults():
    args = jasa_app._parse_args([])
    assert (args.port, args.db, args.host, args.interval) == (8000, "kantor-jasa.db", "127.0.0.1", None)
    args = jasa_app._parse_args(["--port", "9001", "--db", "x.db", "--interval", "10", "--host", "localhost"])
    assert (args.port, args.db, args.host, args.interval) == (9001, "x.db", "localhost", 10.0)
    assert jasa_app.DEFAULT_HOST != "0.0.0.0"


def test_main_serves_on_loopback_by_default(monkeypatch, tmp_path):
    served = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(app=app, **kw))
    jasa_app.main([])
    assert served["host"] == "127.0.0.1" and served["port"] == 8000
    assert (tmp_path / "kantor-jasa.db").exists()
    assert served["app"].state.office is None  # first run: no business yet


@pytest.mark.parametrize(
    "argv, env, expected",
    [
        ([], {}, (True, 3.0, 8.0)),  # the live app paces its staff by default
        (["--no-pacing"], {}, (False, 3.0, 8.0)),
        (["--pace-min", "1", "--pace-max", "2"], {}, (True, 1.0, 2.0)),
        ([], {"KANTOR_PACING": "0"}, (False, 3.0, 8.0)),
    ],
)
def test_main_wires_the_pacing_options(argv, env, expected, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KANTOR_PACING", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    real = jasa_app.create_jasa_runtime

    def spy(db_path, settings=None, **kw):
        seen["settings"] = settings
        return real(db_path, settings=settings, **kw)

    monkeypatch.setattr(jasa_app, "create_jasa_runtime", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    jasa_app.main(argv)
    s = seen["settings"]
    assert (s.pacing_enabled, s.pacing_min_seconds, s.pacing_max_seconds) == expected


@pytest.mark.parametrize("argv", [["--port", "0"], ["--port", "70000"], ["--interval", "0"], ["--interval", "0.5"]])
def test_main_rejects_bad_numbers(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        jasa_app.main(argv)
    assert not (tmp_path / "kantor-jasa.db").exists()


def test_main_refuses_another_business_type_with_a_clear_message(tmp_path):
    from skeleton.core import db

    path = str(tmp_path / "warung.db")
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO business (name, type) VALUES ('Warung', 'warung')")
    conn.close()
    with pytest.raises(SystemExit, match="Cannot start Kantor Jasa"):
        jasa_app.main(["--db", path])


def test_the_run_entry_works_from_any_folder(tmp_path):
    entry = REPO / "apps" / "kantor-jasa" / "app.py"
    result = subprocess.run([sys.executable, str(entry), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for option in ("--port", "--db", "--interval", "--host", "--no-pacing", "--pace-min", "--pace-max", "--real-hours"):
        assert option in result.stdout


def test_readme_documents_the_run_command():
    readme = (REPO / "apps" / "kantor-jasa" / "README.md").read_text(encoding="utf-8")
    assert ".venv/bin/python apps/kantor-jasa/app.py --port 8001" in readme
    assert "127.0.0.1" in readme


def test_no_raw_sql_writes_in_the_jasa_app_modules():
    for name in ("template.py", "app.py"):
        source = (REPO / "apps" / "kantor_jasa" / name).read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", code), name


# ---------------------------------------------------------------------- #
# Demo hours (task 14.1): lively at any hour, unless --real-hours
# ---------------------------------------------------------------------- #
NIGHT = datetime(2025, 3, 10, 2, 0, tzinfo=JAKARTA)  # 02:00 on a Monday


def _actions(runtime):
    return [r[0] for r in runtime.ctx.conn.execute("SELECT action FROM activity ORDER BY id")]


async def _night_ticks(runtime, client, ticks):
    assert (await onboard(client)).status_code == 200
    assert (await set_policy(client)).status_code == 200
    for _ in range(ticks):
        await runtime.ctx.heartbeat.tick()
        runtime.ctx.clock.advance(8)


async def test_booking_requests_arrive_at_night_by_default(db_path):
    runtime = create_jasa_runtime(db_path, settings=Settings(), clock=FakeClock(NIGHT), interval=8, autostart=False)
    try:
        app = create_app(runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://jasa") as c:
            await _night_ticks(runtime, c, ticks=30)
        actions = _actions(runtime)
        # about one request per 2 to 3 ticks, each one confirmed: the booking agent is busy at 02:00
        assert actions.count("Catat permintaan") >= 5 and "Konfirmasi booking" in actions
    finally:
        runtime.ctx.conn.close()


async def test_real_hours_keep_new_booking_requests_out_of_the_night(db_path):
    runtime = create_jasa_runtime(
        db_path, settings=Settings(), clock=FakeClock(NIGHT), interval=8, autostart=False, real_hours=True
    )
    try:
        app = create_app(runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://jasa") as c:
            await _night_ticks(runtime, c, ticks=30)
        assert "Catat permintaan" not in _actions(runtime)
    finally:
        runtime.ctx.conn.close()


def test_real_hours_flag_and_environment_reach_the_runtime(monkeypatch, tmp_path):
    seen = []
    monkeypatch.chdir(tmp_path)
    real = jasa_app.create_jasa_runtime

    def spy(db_path, settings=None, **kw):
        seen.append(kw.get("real_hours"))
        return real(db_path, settings=settings, **kw)

    monkeypatch.setattr(jasa_app, "create_jasa_runtime", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.delenv("KANTOR_REAL_HOURS", raising=False)
    jasa_app.main([])
    jasa_app.main(["--real-hours"])
    monkeypatch.setenv("KANTOR_REAL_HOURS", "true")
    jasa_app.main([])
    monkeypatch.setenv("KANTOR_REAL_HOURS", "off")
    jasa_app.main([])
    assert seen == [False, True, True, False]
    assert jasa_app._parse_args([]).real_hours is False and jasa_app._parse_args(["--real-hours"]).real_hours is True


def test_readme_documents_hours_and_the_pacing_flags():
    readme = (REPO / "apps" / "kantor-jasa" / "README.md").read_text(encoding="utf-8")
    for flag in ("--no-pacing", "--pace-min", "--pace-max", "--real-hours", "KANTOR_REAL_HOURS", "KANTOR_PACING_MIN"):
        assert flag in readme, flag
