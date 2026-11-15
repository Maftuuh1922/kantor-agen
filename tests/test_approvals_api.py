"""Owner approvals through the REST API and the two real apps (task 14.2; Req 5.1, 5.2, 7.1, 8.2).

Real wiring (``create_warung_runtime`` and ``create_jasa_runtime`` on a database file in ``tmp_path``), FakeClock,
the offline LLM, no network. The test drives ``heartbeat.tick()`` by hand. The pure skeleton rules are in
``test_approvals.py``; this file checks the HTTP contract and the Warung and Jasa flows end to end.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from apps.kantor_jasa.app import create_jasa_runtime
from apps.kantor_jasa.roles import BillingRole
from apps.kantor_warung.app import create_warung_runtime
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.core.events import EventBus
from skeleton.office import VisualTracker, create_app
from skeleton.office.sse import FORWARDED_EVENTS, event_stream
from skeleton.policy import approvals as approvals_module
from skeleton.policy import request_approval

REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-FAKE-approvals-key-do-not-leak-0123456789"
OVER_LIMIT = "Melebihi batas harian Rp 50.000; naikkan batas lewat Aturan lalu setujui lagi"
LOW_LIMIT = 50_000
WARUNG_START = datetime(2025, 3, 10, 10, 0, tzinfo=JAKARTA)  # a Monday, the shop is open
JASA_START = datetime(2025, 3, 10, 7, 0, tzinfo=JAKARTA)
APPROVAL_FIELDS = {
    "id", "agent_id", "agent_name", "kind", "subject", "amount_idr", "summary", "reason", "status", "created_at",
}


class Spy:
    """Wraps ``bus.publish`` so a test can read every event, without an event loop."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[dict] = []
        original = bus.publish

        def publish(event):
            self.events.append(dict(event))
            original(event)

        bus.publish = publish  # type: ignore[method-assign]

    def of(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["type"] == event_type]


def table(conn, name: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY id")]


def decisions(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM activity WHERE action = 'Keputusan pemilik' ORDER BY id")]


async def decide(client, approval_id, approve=True, note=None):
    body = {"approve": approve}
    if note is not None:
        body["note"] = note
    return await client.post(f"/api/approvals/{approval_id}/decision", json=body)


def state_of(conn) -> tuple:
    """Everything a refused or invalid decision must leave alone."""
    return (
        table(conn, "approval"), table(conn, "spend_ledger"),
        table(conn, "purchase_order") if _has(conn, "purchase_order") else [],
        table(conn, "invoice") if _has(conn, "invoice") else [],
        table(conn, "activity"),
    )


def _has(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


# ---------------------------------------------------------------------- #
# Kantor Warung
# ---------------------------------------------------------------------- #
@pytest.fixture
def clock():
    return FakeClock(WARUNG_START)


@pytest.fixture
async def warung(tmp_path, clock):
    """An onboarded Warung office with a low daily limit and three purchase orders waiting for the owner."""
    runtime = create_warung_runtime(
        str(tmp_path / "warung.db"), settings=Settings(llm_api_key=FAKE_KEY), clock=clock, autostart=False
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://warung") as client:
        assert (await client.post("/api/onboarding", json={"name": "Warung Bu Tini", "type": "warung"})).status_code == 200
        assert (await client.put("/api/policy", json={"daily_limit_idr": LOW_LIMIT, "blocked_actions": []})).status_code == 200
        runtime.spy = Spy(runtime.ctx.bus)
        results = await runtime.ctx.heartbeat.tick()
        assert [r.status for r in results].count("pending") == 3
        runtime.client = client
        yield runtime
    runtime.ctx.conn.close()


async def pending_by_subject(client) -> dict[str, dict]:
    return {a["subject"]: a for a in (await client.get("/api/approvals")).json()}


async def test_the_stock_agent_files_one_request_per_pending_order(warung):
    client, conn = warung.client, warung.ctx.conn
    listed = (await client.get("/api/approvals")).json()
    assert len(listed) == 3
    for item in listed:
        assert set(item) == APPROVAL_FIELDS  # no raw detail, no secrets, nothing else
        assert item["kind"] == "purchase_order" and item["status"] == "pending"
        assert item["agent_name"] == "Budi" and item["amount_idr"] > LOW_LIMIT
        assert item["summary"] and item["reason"] and item["subject"].startswith("Pesan stok ")
    assert [a["id"] for a in listed] == sorted(a["id"] for a in listed)  # oldest first
    assert [o["status"] for o in table(conn, "purchase_order")] == ["pending"] * 3
    assert table(conn, "spend_ledger") == []
    # Idempotent: more ticks file nothing new.
    for _ in range(3):
        warung.ctx.clock.advance(300)
        await warung.ctx.heartbeat.tick()
    assert len((await client.get("/api/approvals")).json()) == 3 and len(table(conn, "purchase_order")) == 3
    assert (await client.get("/api/summary")).json()["pending_approvals"] == 3
    assert (await client.get("/api/office")).json()["pending_approvals"] == 3
    stock = next(a for a in (await client.get("/api/office")).json()["agents"] if a["name"] == "Budi")
    panel = (await client.get(f"/api/agents/{stock['id']}")).json()
    assert [a["id"] for a in panel["pending_approvals"]] == [a["id"] for a in listed]


async def test_an_agent_with_a_pending_request_stays_waiting_until_the_owner_decides(warung):
    client, conn = warung.client, warung.ctx.conn
    stock_id = conn.execute("SELECT id FROM agent WHERE role = 'stock'").fetchone()[0]
    for _ in range(3):  # later ticks that do nothing must not turn the amber marker off
        warung.ctx.clock.advance(300)
        await warung.ctx.heartbeat.tick()
        assert (await client.get(f"/api/agents/{stock_id}")).json()["state"] == "waiting"
    # The owner decides all three: the agent is free again.
    for item in (await client.get("/api/approvals")).json():
        assert (await decide(client, item["id"], False)).status_code == 200
    assert (await client.get(f"/api/agents/{stock_id}")).json()["state"] == "idle"


async def test_approving_over_the_limit_is_refused_and_changes_nothing(warung):
    client, conn = warung.client, warung.ctx.conn
    target = (await pending_by_subject(client))["Pesan stok Kecap Manis 275 ml"]  # Rp 92.000 > Rp 50.000
    before = state_of(conn)
    response = await decide(client, target["id"], True)
    assert response.status_code == 422
    assert response.json() == {"detail": OVER_LIMIT, "status": "pending"}
    assert state_of(conn) == before  # no ledger row, no order change, no activity row, still pending
    assert not warung.spy.of("approval_changed")[3:]  # only the three "pending" events from the requests
    assert FAKE_KEY not in response.text


async def test_after_the_owner_raises_the_limit_the_same_request_is_approved(warung):
    client, conn, spy = warung.client, warung.ctx.conn, warung.spy
    target = (await pending_by_subject(client))["Pesan stok Kecap Manis 275 ml"]
    assert (await decide(client, target["id"], True)).status_code == 422
    assert (await client.put("/api/policy", json={"daily_limit_idr": 100_000, "blocked_actions": []})).status_code == 200
    spy.events.clear()
    response = await decide(client, target["id"], True, note="  oke  ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"id", "status", "message", "cost_idr", "pending_count"}
    assert (body["id"], body["status"], body["cost_idr"], body["pending_count"]) == (target["id"], "approved", 92_000, 2)
    assert "Kecap Manis" in body["message"]
    # The order is placed, paid through the ledger, and logged.
    orders = {o["id"]: o for o in table(conn, "purchase_order")}
    assert sorted(o["status"] for o in orders.values()) == ["pending", "pending", "placed"]
    assert [(r["amount_idr"]) for r in table(conn, "spend_ledger")] == [92_000]
    assert (await client.get("/api/office")).json()["spent_today_idr"] == 92_000
    [row] = decisions(conn)
    assert (row["cost_idr"], row["status"]) == (92_000, "done") and "menyetujui" in row["reason"] and "Rp 92.000" in row["reason"]
    stored = conn.execute("SELECT status, decided_by, decision_note FROM approval WHERE id = ?", (target["id"],)).fetchone()
    assert tuple(stored) == ("approved", "owner", "oke")
    # Events: the activity row and approval_changed, both on the bus.
    assert [e["type"] for e in spy.events if e["type"] in ("activity", "approval_changed")] == ["activity", "approval_changed"]
    [changed] = spy.of("approval_changed")
    assert changed == {
        "type": "approval_changed", "approval_id": target["id"], "agent_id": target["agent_id"],
        "status": "approved", "pending_count": 2,
    }
    # Decided once: a second decision, either way, is a conflict and writes nothing.
    before = state_of(conn)
    for approve in (True, False):
        again = await decide(client, target["id"], approve)
        assert again.status_code == 409
        assert again.json() == {"detail": "approval is not pending", "status": "approved"}
    assert state_of(conn) == before
    assert target["id"] not in [a["id"] for a in (await client.get("/api/approvals")).json()]


async def test_the_budget_is_shared_by_all_approvals_of_the_day(warung):
    client, conn = warung.client, warung.ctx.conn
    items = await pending_by_subject(client)
    await client.put("/api/policy", json={"daily_limit_idr": 300_000, "blocked_actions": []})
    assert (await decide(client, items["Pesan stok Kecap Manis 275 ml"]["id"], True)).status_code == 200  # Rp 92.000
    milk = await decide(client, items["Pesan stok Susu Kental Manis"]["id"], True)  # Rp 216.000: 308.000 > 300.000
    assert milk.status_code == 422 and "Rp 300.000" in milk.json()["detail"]
    assert [r["amount_idr"] for r in table(conn, "spend_ledger")] == [92_000]
    assert (await decide(client, items["Pesan stok Susu Kental Manis"]["id"], False)).status_code == 200


async def test_rejecting_cancels_the_order_and_starts_a_24_hour_cooldown(warung, clock):
    client, conn = warung.client, warung.ctx.conn
    target = (await pending_by_subject(client))["Pesan stok Gula Pasir 1 kg"]
    response = await decide(client, target["id"], False, note="terlalu mahal")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["cost_idr"], body["pending_count"]) == ("rejected", 0, 2)
    assert table(conn, "spend_ledger") == []
    gula_id = conn.execute("SELECT id FROM product WHERE name = 'Gula Pasir 1 kg'").fetchone()[0]

    def gula_orders():
        return [o["status"] for o in table(conn, "purchase_order") if o["product_id"] == gula_id]

    assert gula_orders() == ["cancelled"]
    [row] = decisions(conn)
    assert (row["cost_idr"], row["status"]) == (0, "rejected") and "menolak" in row["reason"] and "terlalu mahal" in row["reason"]
    # Within 24 hours of the clock the stock agent does not ask about this product again.
    for _ in range(4):
        clock.advance(3600)
        await warung.ctx.heartbeat.tick()
    assert "Pesan stok Gula Pasir 1 kg" not in await pending_by_subject(client)
    assert gula_orders() == ["cancelled"]
    # After the cool-down it may ask again (a new request, a new order).
    clock.advance(24 * 3600)
    await warung.ctx.heartbeat.tick()
    again = (await pending_by_subject(client)).get("Pesan stok Gula Pasir 1 kg")
    assert again is not None and again["id"] != target["id"]
    assert gula_orders() == ["cancelled", "pending"]


async def test_an_approved_order_is_delivered_later_by_the_stock_agent(warung):
    client, conn = warung.client, warung.ctx.conn
    target = (await pending_by_subject(client))["Pesan stok Kecap Manis 275 ml"]
    order_id = conn.execute("SELECT id FROM purchase_order WHERE cost_idr = 92000").fetchone()[0]
    await client.put("/api/policy", json={"daily_limit_idr": 100_000, "blocked_actions": []})
    assert (await decide(client, target["id"], True)).status_code == 200
    assert conn.execute("SELECT status FROM purchase_order WHERE id = ?", (order_id,)).fetchone()[0] == "placed"
    for _ in range(48):  # the delivery arrives after the usual lead time
        warung.ctx.clock.advance(3600)
        await warung.ctx.heartbeat.tick()
    row = conn.execute("SELECT status, received_at FROM purchase_order WHERE id = ?", (order_id,)).fetchone()
    assert row["status"] == "received" and row["received_at"] is not None
    # Paid once, at approval. (Later rows are fresh reorders of the same product once it sold down again.)
    assert table(conn, "spend_ledger")[0]["amount_idr"] == 92_000


@pytest.mark.parametrize(
    "body",
    [
        {}, {"approve": 1}, {"approve": 0}, {"approve": "true"}, {"approve": None}, {"approve": [True]},
        {"approve": True, "note": "x" * 201}, {"approve": True, "note": 5}, {"approve": True, "note": ["a"]},
        {"approve": True, "extra": 1},
    ],
)
async def test_a_bad_decision_body_is_422_and_changes_nothing(warung, body):
    client, conn = warung.client, warung.ctx.conn
    first = (await client.get("/api/approvals")).json()[0]["id"]
    before = state_of(conn)
    response = await client.post(f"/api/approvals/{first}/decision", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)  # a validation problem, not the over-limit message
    assert state_of(conn) == before


async def test_a_note_of_exactly_200_characters_is_accepted(warung):
    client = warung.client
    first = (await client.get("/api/approvals")).json()[0]["id"]
    assert (await decide(client, first, False, note="n" * 200)).status_code == 200


async def test_not_json_and_a_non_numeric_id_are_422(warung):
    client = warung.client
    assert (await client.post("/api/approvals/1/decision", content=b"not json", headers={"content-type": "application/json"})).status_code == 422
    assert (await client.post("/api/approvals/abc/decision", json={"approve": True})).status_code == 422


@pytest.mark.parametrize("approval_id", [999, 0, -1, 2**63, 2**80])
async def test_an_unknown_approval_is_404(warung, approval_id):
    before = state_of(warung.ctx.conn)
    response = await decide(warung.client, approval_id, True)
    assert response.status_code == 404 and response.json() == {"detail": "approval not found"}
    assert state_of(warung.ctx.conn) == before


async def test_a_database_error_is_503_without_details(warung, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked: /secret/path.db")

    monkeypatch.setattr(approvals_module, "decide", boom)
    response = await decide(warung.client, 1, True)
    assert response.status_code == 503
    assert "secret" not in response.text and "locked" not in response.text


async def test_no_secret_is_in_any_approval_response(warung):
    client = warung.client
    texts = [(await client.get("/api/approvals")).text]
    first = (await client.get("/api/approvals")).json()[0]["id"]
    texts += [(await decide(client, first, True)).text, (await decide(client, first, True)).text]  # 422 or 200, then 409
    texts += [(await decide(client, 999, True)).text, (await client.post(f"/api/approvals/{first}/decision", json={"approve": 1})).text]
    for text in texts:
        assert FAKE_KEY not in text and "llm_api_key" not in text and "sk-" not in text


async def test_decisions_are_logged_in_the_activity_feed(warung):
    client = warung.client
    first = (await client.get("/api/approvals")).json()[0]["id"]
    await decide(client, first, False)
    feed = (await client.get("/api/activity?limit=50")).json()
    assert [r["action"] for r in feed].count("Keputusan pemilik") == 1


async def test_the_decision_events_reach_the_sse_stream():
    assert "approval_changed" in FORWARDED_EVENTS
    conn = db.connect(":memory:")
    try:
        db.init_schema(conn)
        conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'X', 'warung')")
        conn.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (1, 1, 'stock', 'Budi', 0)")
        bus, clock = EventBus(), FakeClock(WARUNG_START)
        stream = event_stream(bus, VisualTracker(conn, 1, bus, clock), keepalive_seconds=5)
        assert (await asyncio.wait_for(anext(stream), 1)).startswith("event: snapshot")
        waiting = asyncio.ensure_future(asyncio.wait_for(anext(stream), 1))
        await asyncio.sleep(0)
        approval_id = request_approval(conn, 1, 1, "k", "Sesuatu", {}, 0, clock, bus=bus)
        frame = await waiting
        assert frame.startswith("event: approval_changed\n")
        payload = json.loads(frame.split("data: ", 1)[1])
        assert (payload["approval_id"], payload["status"], payload["pending_count"]) == (approval_id, "pending", 1)
        await stream.aclose()
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
# Kantor Jasa
# ---------------------------------------------------------------------- #
@pytest.fixture
async def jasa(tmp_path):
    clock = FakeClock(JASA_START)
    runtime = create_jasa_runtime(
        str(tmp_path / "jasa.db"), settings=Settings(llm_api_key=FAKE_KEY), clock=clock, autostart=False
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://jasa") as client:
        assert (await client.post("/api/onboarding", json={"name": "Salon Mawar", "type": "jasa"})).status_code == 200
        assert (await client.put("/api/policy", json={"daily_limit_idr": LOW_LIMIT, "blocked_actions": []})).status_code == 200
        runtime.spy = Spy(runtime.ctx.bus)
        await runtime.ctx.heartbeat.tick()
        runtime.client = client
        yield runtime
    runtime.ctx.conn.close()


def invoice(conn, invoice_id) -> dict:
    return dict(conn.execute("SELECT * FROM invoice WHERE id = ?", (invoice_id,)).fetchone())


async def test_a_large_draft_invoice_waits_for_the_owner(jasa):
    client, conn = jasa.client, jasa.ctx.conn
    listed = (await client.get("/api/approvals")).json()
    assert [(a["kind"], a["subject"], a["amount_idr"]) for a in listed] == [
        ("invoice", "Invoice #11 Ani Rahayu", 650_000), ("invoice", "Invoice #9 Sri Wahyuni", 650_000),
    ]
    assert all(set(a) == APPROVAL_FIELDS and a["agent_name"] == "Agus" for a in listed)
    assert [invoice(conn, i)["status"] for i in (9, 11)] == ["draft", "draft"]
    billing_id = conn.execute("SELECT id FROM agent WHERE role = 'billing'").fetchone()[0]
    assert (await client.get(f"/api/agents/{billing_id}")).json()["state"] == "waiting"
    for _ in range(3):  # no duplicates on later ticks
        jasa.ctx.clock.advance(3600)
        await jasa.ctx.heartbeat.tick()
    assert len((await client.get("/api/approvals")).json()) == 2


async def test_the_owner_approves_a_large_invoice_and_it_becomes_immutable(jasa):
    client, conn, spy = jasa.client, jasa.ctx.conn, jasa.spy
    target = next(a for a in (await client.get("/api/approvals")).json() if a["subject"].startswith("Invoice #11"))
    spy.events.clear()
    response = await decide(client, target["id"], True, note="ok, pelanggan tetap")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["cost_idr"], body["pending_count"]) == ("approved", 0, 1)  # an invoice spends no money
    row = invoice(conn, 11)
    assert (row["status"], row["approved_by"], row["amount_idr"]) == ("approved", "owner", 650_000)
    assert row["approved_at"] is not None
    assert table(conn, "spend_ledger") == []  # the daily spending limit is not involved
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE invoice SET amount_idr = 1 WHERE id = 11")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM invoice WHERE id = 11")
    [entry] = decisions(conn)
    assert (entry["status"], entry["cost_idr"]) == ("done", 0) and "Rp 650.000" in entry["reason"]
    [changed] = spy.of("approval_changed")
    assert (changed["approval_id"], changed["status"], changed["pending_count"]) == (target["id"], "approved", 1)
    # The billing agent never touches it again, on any later tick.
    for _ in range(4):
        jasa.ctx.clock.advance(7200)
        await jasa.ctx.heartbeat.tick()
    assert invoice(conn, 11)["approved_by"] == "owner" and invoice(conn, 11)["amount_idr"] == 650_000
    assert (await decide(client, target["id"], True)).status_code == 409


async def test_rejecting_a_large_invoice_voids_it_and_it_is_not_invoiced_again(jasa):
    client, conn = jasa.client, jasa.ctx.conn
    target = next(a for a in (await client.get("/api/approvals")).json() if a["subject"].startswith("Invoice #9"))
    response = await decide(client, target["id"], False)
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert invoice(conn, 9)["status"] == "void" and invoice(conn, 9)["approved_by"] is None
    for _ in range(4):
        jasa.ctx.clock.advance(7200)
        await jasa.ctx.heartbeat.tick()
    assert invoice(conn, 9)["status"] == "void"
    assert conn.execute("SELECT COUNT(*) FROM invoice WHERE booking_id = 9").fetchone()[0] == 1
    assert all(a["subject"] != "Invoice #9 Sri Wahyuni" for a in (await client.get("/api/approvals")).json())


async def test_an_invoice_settled_by_policy_leaves_the_owner_queue(jasa):
    """The auto-approve limit was raised after the owner was asked: the old question must not linger."""
    client, conn = jasa.client, jasa.ctx.conn
    billing = next(r for r in jasa.ctx.heartbeat._roles.values() if isinstance(r, BillingRole))
    billing.auto_approve_limit_idr = 700_000
    await jasa.ctx.heartbeat.tick()
    assert (invoice(conn, 9)["status"], invoice(conn, 9)["approved_by"]) == ("approved", "policy")
    assert (await client.get("/api/approvals")).json() == []
    rows = table(conn, "approval")
    assert {r["status"] for r in rows} == {"failed"} and all(r["decided_by"] is None for r in rows)
    assert decisions(conn) == []  # the owner decided nothing


# ---------------------------------------------------------------------- #
# Boundaries
# ---------------------------------------------------------------------- #
def test_the_approval_module_knows_no_app_and_no_business_type():
    path = REPO / "skeleton" / "policy" / "approvals.py"
    source = path.read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m.split(".")[0] == "apps" or m.startswith("skeleton.runtime")}
    for word in ("warung", "jasa", "stok", "product", "sale"):
        assert word not in source.lower(), word
