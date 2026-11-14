"""Smoke tests for Kantor Jasa, the way a new user meets it (Req 3.2, 10.1, with 7.1, 5.1, 5.4, 10.2, 10.3).

Three short checks, no network and no real sleeping:

* ``test_a_new_user_gets_a_working_office``: build the real runtime on a database file, onboard through the
  API, set the spending rules, move the clock to a working hour and run two heartbeat ticks by hand. Then
  check the invariants that must hold after any run.
* ``test_the_seed_alone_is_consistent``: the demo data right after onboarding, before any agent has run.
* ``test_the_run_entry_prints_its_usage_from_any_folder``: ``--help`` as a subprocess.

No sleeping: ``Settings`` has pacing off (``pacing_enabled=False``), so the heartbeat never waits, and the
ticks are driven by hand instead of ``run_forever``. No network: no API key means the offline fake model.
The detailed behaviour of the roles, the seed and the app wiring is tested in ``test_jasa_app.py`` and
``test_jasa_seed.py``; this file only checks that the whole thing hangs together.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.kantor_jasa import data
from apps.kantor_jasa.app import create_jasa_runtime
from apps.kantor_jasa.roles import DEFAULT_AUTO_APPROVE_LIMIT_IDR
from apps.kantor_jasa.template import JASA_TEMPLATE
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.office import create_app
from skeleton.office.onboarding import bootstrap

REPO = Path(__file__).resolve().parent.parent
START = datetime(2025, 3, 10, 7, 0, tzinfo=JAKARTA)  # a Monday, 07:00 Jakarta time
WORKING_HOUR = datetime(2025, 3, 10, 10, 0, tzinfo=JAKARTA)  # the shop is open
DAILY_LIMIT_IDR = 100_000
ACTIVITY_FIELDS = ("agent_id", "action", "reason", "cost_idr", "status", "created_at")
INVOICE_TRIGGERS = {
    "invoice_immutable_after_approval",
    "invoice_draft_leaves_unedited",
    "invoice_status_transition",
    "invoice_no_delete_after_draft",
}
# What an approved or paid invoice may never lose. Only status and paid_at may move (approved -> paid).
FROZEN_INVOICE_FIELDS = (
    "id", "business_id", "customer_id", "booking_id", "description", "amount_idr",
    "approved_by", "approved_at", "created_at",
)


def _span(booking: dict) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(booking["starts_at"])
    return start, start + timedelta(minutes=booking["duration_min"])


def _assert_no_overlap(bookings: list[dict]) -> None:
    by_resource: dict[str, list[dict]] = {}
    for booking in bookings:
        if booking["status"] in ("confirmed", "done"):
            by_resource.setdefault(booking["resource"], []).append(booking)
    assert by_resource, "expected confirmed bookings"
    for resource, items in by_resource.items():
        items.sort(key=lambda b: _span(b)[0])
        for earlier, later in zip(items, items[1:]):
            assert _span(earlier)[1] <= _span(later)[0], f"two bookings overlap on {resource}"


def test_a_new_user_gets_a_working_office(tmp_path, monkeypatch):
    monkeypatch.setenv("KANTOR_LLM_API_KEY", "sk-SMOKE-canary-key-0123456789")  # must not matter or leak
    clock = FakeClock(START)
    settings = Settings(pacing_enabled=False)  # no think time: nothing to sleep for
    runtime = create_jasa_runtime(str(tmp_path / "jasa.db"), settings=settings, clock=clock, autostart=False)
    client = TestClient(create_app(runtime))
    seen: list[str] = []

    def call(method: str, path: str, **kwargs):
        response = client.request(method, path, **kwargs)
        seen.append(response.text)
        return response

    try:
        # First run: the office asks for onboarding, then the owner names the business.
        assert call("GET", "/api/office").json()["onboarding_required"] is True
        onboarded = call("POST", "/api/onboarding", json={"name": "Salon Mawar", "type": "jasa"})
        assert onboarded.status_code == 200, onboarded.text
        assert [a["name"] for a in onboarded.json()["agents"]] == ["Dewi", "Agus", "Rina"]
        policy = call("PUT", "/api/policy", json={"daily_limit_idr": DAILY_LIMIT_IDR, "blocked_actions": []})
        assert policy.status_code == 200 and policy.json()["is_set"] is True

        ctx = runtime.ctx
        assert ctx is not None and ctx.settings.pacing_enabled is False
        conn, business_id = ctx.conn, ctx.business_id
        agent_ids = {int(a["id"]) for a in onboarded.json()["agents"]}
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0  # the seed writes no activity

        # Move to a working hour, remember the money documents, then let the staff work.
        clock.set(WORKING_HOUR)
        invoices_before = {i["id"]: i for i in data.list_invoices(conn, business_id)}
        frozen_before = {i: row for i, row in invoices_before.items() if row["status"] in ("approved", "paid")}
        assert frozen_before, "the seed must hold approved or paid invoices"

        first = asyncio.run(ctx.heartbeat.tick())
        assert first, "the first tick handled no task"
        clock.advance(300)
        asyncio.run(ctx.heartbeat.tick())

        # At least one agent wrote an activity row (Req 3.2).
        rows = call("GET", "/api/activity?limit=500").json()
        assert rows, "no agent wrote an activity row"
        assert {r["agent_id"] for r in rows} <= agent_ids

        # Every row says who, what, why, how much, how it went and when (Req 7.1).
        for row in rows:
            for field in ACTIVITY_FIELDS:
                assert row.get(field) is not None, f"activity row without {field}: {row}"
            assert row["action"].strip() and row["reason"].strip() and row["status"].strip()
            assert isinstance(row["cost_idr"], int) and row["cost_idr"] >= 0
            assert datetime.fromisoformat(row["created_at"]).tzinfo is not None

        # No double booking on one chair.
        _assert_no_overlap(data.list_bookings(conn, business_id))

        # Approved and paid invoices were not touched (Req 5.1, 6.2). An approved one may become paid.
        invoices_after = {i["id"]: i for i in data.list_invoices(conn, business_id)}
        for invoice_id, before in frozen_before.items():
            after = invoices_after[invoice_id]  # still there
            assert {k: after[k] for k in FROZEN_INVOICE_FIELDS} == {k: before[k] for k in FROZEN_INVOICE_FIELDS}
            if before["status"] == "paid":
                assert after == before
            else:
                assert after["status"] in ("approved", "paid")

        # Messages wait in the outbox. Nothing is sent (Req 5.4).
        outbox = call("GET", "/api/outbox").json()
        assert outbox, "the due payment reminders should be queued"
        assert all(m["status"] == "pending" for m in outbox)
        assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM outbox")} == {"pending"}

        # Spending stays inside the owner's limit; Jasa staff spend nothing at all.
        office = call("GET", "/api/office").json()
        assert 0 <= office["spent_today_idr"] <= office["policy"]["daily_limit_idr"] == DAILY_LIMIT_IDR
        ledger = conn.execute("SELECT COALESCE(SUM(amount_idr), 0) FROM spend_ledger").fetchone()[0]
        assert ledger <= DAILY_LIMIT_IDR

        # The API key never leaves the server (Req 10.2).
        assert office["demo_mode"] is True
        everything = "\n".join(seen)
        assert "sk-SMOKE" not in everything and "api_key" not in everything.lower()
    finally:
        client.close()
        if runtime.ctx is not None:
            runtime.ctx.conn.close()


def test_the_seed_alone_is_consistent(tmp_path):
    clock = FakeClock(START)
    conn = db.connect(str(tmp_path / "seed.db"), check_same_thread=False)
    try:
        db.init_schema(conn)
        business_id = bootstrap(conn, JASA_TEMPLATE, "Salon Mawar", clock)

        # Customers and bookings.
        customers = data.list_customers(conn, business_id)
        assert len(customers) == 8 and all(c["name"] and c["phone"] for c in customers)
        bookings = data.list_bookings(conn, business_id)
        assert {b["status"] for b in bookings} >= {"requested", "confirmed", "done"}
        customer_ids = {c["id"] for c in customers}
        assert all(b["customer_id"] in customer_ids for b in bookings)
        _assert_no_overlap(bookings)

        # A payment reminder is due: approved, unpaid for more than a week.
        due = data.unpaid_overdue(conn, business_id, clock, days=7)
        assert due and all(i["status"] == "approved" for i in due)

        # One draft invoice is above the auto-approve limit, so it waits for the owner.
        drafts = data.list_invoices(conn, business_id, status="draft")
        assert len(drafts) == 1 and drafts[0]["amount_idr"] > DEFAULT_AUTO_APPROVE_LIMIT_IDR
        assert drafts[0]["approved_by"] is None

        # The immutability triggers are installed and fire.
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert INVOICE_TRIGGERS <= triggers
        approved = data.list_invoices(conn, business_id, status="approved")[0]
        with pytest.raises(Exception, match="immutable"):
            conn.execute("UPDATE invoice SET amount_idr = amount_idr + 1 WHERE id = ?", (approved["id"],))

        # The seed is only data: no agent has acted yet.
        assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_run_entry_prints_its_usage_from_any_folder(tmp_path):
    entry = REPO / "apps" / "kantor-jasa" / "app.py"
    result = subprocess.run(
        [sys.executable, str(entry), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "--port" in result.stdout and "--db" in result.stdout
    assert not (tmp_path / "kantor-jasa.db").exists()  # --help must not create a database
