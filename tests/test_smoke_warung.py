"""Smoke test for Kantor Warung, the way a new owner meets it (Req 3.2, 10.1; Req 7.1 for the log rows).

Three short checks, no network and no real waiting:

1. Fresh database file, onboarding, spending rules, one heartbeat tick at a working hour. At least one of
   the four staff must have written an activity row, and the books must be sane afterwards.
2. The seed data alone is internally consistent.
3. ``apps/kantor-warung/app.py --help`` runs from any folder.

Pacing is off (``Settings.pacing_enabled=False``), so the tick never sleeps. The LLM is the offline fake.
Deeper behavior is in ``test_warung_app.py``, ``test_warung_roles.py`` and ``test_warung_data.py``.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from apps.kantor_warung import app as warung_app
from apps.kantor_warung.app import create_warung_runtime
from apps.kantor_warung.seed import CATALOG, HISTORY_DAYS
from apps.kantor_warung.template import WARUNG_TEMPLATE
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.office import create_app
from skeleton.office.onboarding import bootstrap
from skeleton.runtime.llm import FakeLLMClient

REPO = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-FAKE-smoke-key-do-not-leak-0123456789"
DAILY_LIMIT_IDR = 100_000
ACTIVITY_FIELDS = ("agent_id", "action", "reason", "cost_idr", "status", "created_at")
OUTBOX_PENDING = "pending"


def test_one_heartbeat_after_onboarding_makes_the_staff_work(tmp_path, monkeypatch):
    # A key is set so the response checks mean something; the client is the offline fake, so nothing goes out.
    monkeypatch.setattr(warung_app, "build_llm_client", lambda settings: FakeLLMClient())
    settings = Settings(llm_api_key=FAKE_KEY, pacing_enabled=False)
    clock = FakeClock(datetime(2025, 3, 10, 6, 0, tzinfo=JAKARTA))  # a Monday, before the shop opens
    runtime = create_warung_runtime(str(tmp_path / "warung.db"), settings=settings, clock=clock, autostart=False)
    try:
        with TestClient(create_app(runtime)) as http:
            # First run, as the owner sees it: onboarding, then the spending rules.
            assert http.get("/api/office").json()["onboarding_required"] is True
            assert http.post("/api/onboarding", json={"name": "Warung Bu Tini", "type": "warung"}).status_code == 200
            policy = {"daily_limit_idr": DAILY_LIMIT_IDR, "blocked_actions": []}
            assert http.put("/api/policy", json=policy).status_code == 200

            clock.advance(4 * 3600)  # 10:00, the shop is open
            results = asyncio.run(runtime.ctx.heartbeat.tick())
            assert results, "the heartbeat produced no work at all"

            office_response = http.get("/api/office")
            activity_response = http.get("/api/activity?limit=500")
            assert office_response.status_code == 200 and activity_response.status_code == 200
            office, rows = office_response.json(), activity_response.json()
            agent_ids = {a["id"] for a in office["agents"]}
            assert len(agent_ids) == 4

            # At least one agent wrote an activity row, and every row is complete (Req 7.1).
            assert rows and {r["agent_id"] for r in rows} & agent_ids
            for row in rows:
                assert all(name in row for name in ACTIVITY_FIELDS), row
                assert row["agent_id"] in agent_ids
                assert isinstance(row["action"], str) and row["action"]
                assert isinstance(row["reason"], str) and row["reason"].strip()
                assert isinstance(row["cost_idr"], int) and row["cost_idr"] >= 0
                assert isinstance(row["status"], str) and row["status"]
                assert datetime.fromisoformat(row["created_at"]).tzinfo is not None

            # The books are sane.
            conn = runtime.ctx.conn
            assert conn.execute("SELECT COUNT(*) FROM product WHERE qty < 0").fetchone()[0] == 0
            assert 0 <= office["spent_today_idr"] <= DAILY_LIMIT_IDR
            assert office["policy"]["daily_limit_idr"] == DAILY_LIMIT_IDR
            outbox = conn.execute("SELECT status FROM outbox").fetchall()
            assert all(r["status"] == OUTBOX_PENDING for r in outbox)
            assert all(m["status"] == OUTBOX_PENDING for m in http.get("/api/outbox").json())

            # The key stays on the server (Req 10.2).
            assert office["demo_mode"] is False
            assert FAKE_KEY not in office_response.text and FAKE_KEY not in activity_response.text
            assert "llm_api_key" not in office_response.text
    finally:
        runtime.ctx.conn.close()


def test_seed_data_is_internally_consistent(tmp_path):
    conn = db.connect(str(tmp_path / "seed.db"))
    try:
        db.init_schema(conn)
        clock = FakeClock(datetime(2025, 3, 10, 6, 0, tzinfo=JAKARTA))
        business_id = bootstrap(conn, WARUNG_TEMPLATE, "Warung Bu Tini", clock)

        products = conn.execute("SELECT * FROM product WHERE business_id = ?", (business_id,)).fetchall()
        assert len(products) == len(CATALOG) > 0
        assert len({p["name"] for p in products}) == len(products)
        assert {p["name"] for p in products} == {item.name for item in CATALOG}
        assert any(p["qty"] < p["min_qty"] for p in products), "no product starts below its minimum"
        for p in products:
            assert p["qty"] >= 0 and p["min_qty"] >= 0
            assert p["unit_price_idr"] > 0 and 0 <= p["unit_cost_idr"] <= p["unit_price_idr"], p["name"]

        # A week of sales exists, one per basket, spread over the past days only.
        sales = conn.execute("SELECT id, total_idr, created_at FROM sale WHERE business_id = ?", (business_id,)).fetchall()
        assert len(sales) > 7 * HISTORY_DAYS
        days = {datetime.fromisoformat(s["created_at"]).astimezone(JAKARTA).date() for s in sales}
        assert len(days) == HISTORY_DAYS
        assert max(days) < clock.now().astimezone(JAKARTA).date()
        for sale in sales:
            items = conn.execute("SELECT * FROM sale_item WHERE sale_id = ?", (sale["id"],)).fetchall()
            assert items, "a sale without lines"
            assert all(i["qty"] > 0 and i["unit_price_idr"] >= 0 and i["discount_idr"] >= 0 for i in items)
            assert sale["total_idr"] >= 0
            assert sale["total_idr"] == sum(i["qty"] * i["unit_price_idr"] - i["discount_idr"] for i in items)

        # Nothing else has happened yet: no log, no money spent, no policy, no messages, no orders.
        for table in ("activity", "spend_ledger", "outbox", "purchase_order", "policy"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        agents = conn.execute("SELECT name, desk_index FROM agent WHERE business_id = ? ORDER BY desk_index", (business_id,))
        assert [(a["name"], a["desk_index"]) for a in agents] == [("Sari", 0), ("Budi", 1), ("Wati", 2), ("Joko", 3)]
    finally:
        conn.close()


def test_help_runs_from_another_folder(tmp_path):
    entry = REPO / "apps" / "kantor-warung" / "app.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # the entry must find the repo itself
    result = subprocess.run(
        [sys.executable, str(entry), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "--port" in result.stdout and "--db" in result.stdout
    assert not list(tmp_path.iterdir()), "--help must not create a database file"
