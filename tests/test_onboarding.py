"""Tests for ``skeleton.office.onboarding`` and the Warung business type (Req 1.1, 1.2, 1.3, 10.1).

``bootstrap`` is used on a real in-memory database. The Warung template is the real one; a tiny
template of our own is used for the failure paths. No network, no fake of the code under test.
"""
from __future__ import annotations

import inspect
import json
from datetime import date, timedelta

import pytest

import skeleton.office.onboarding as onboarding
from apps.kantor_warung import data
from apps.kantor_warung.roles import CashierRole, FinanceRole, MarketingRole, StockRole
from apps.kantor_warung.seed import CATALOG, HISTORY_DAYS, plan_history
from apps.kantor_warung.template import WARUNG_TEMPLATE
from skeleton.core import db
from skeleton.core.clock import FakeClock
from skeleton.office.onboarding import (
    AgentSpec,
    AlreadyOnboarded,
    BusinessTemplate,
    InvalidBusinessName,
    TemplateRegistry,
    UnknownBusinessType,
    bootstrap,
    normalize_business_name,
)
from skeleton.policy.engine import authorize, set_policy
from skeleton.runtime import Task, TaskKind

TABLES_WITH_BUSINESS_DATA = ("business", "agent", "agent_label", "policy", "product", "sale", "sale_item",
                             "purchase_order", "spend_ledger", "activity", "outbox")


@pytest.fixture
def conn():
    c = db.connect(":memory:", check_same_thread=False)
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def clock():
    return FakeClock()


def counts(conn) -> dict[str, int]:
    onboarding.ensure_label_table(conn)
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES_WITH_BUSINESS_DATA}


def toy_template(seed=None, key="toko") -> BusinessTemplate:
    """Two agents, and a seed that writes one product through its own transaction (like a data layer)."""

    def default_seed(conn, business_id, clock):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO product (business_id, name, qty, min_qty, unit_price_idr) VALUES (?, 'Gula', 5, 2, 1000)",
                (business_id,),
            )

    return BusinessTemplate(
        key=key,
        label="Toko",
        description="Toko contoh",
        agents=(AgentSpec("alpha", "Ani", "Kasir"), AgentSpec("beta", "Bima", "Gudang")),
        seed=seed or default_seed,
        build_roles=lambda conn, business_id, clock: {},
    )


# ---------------------------------------------------------------------- #
# bootstrap
# ---------------------------------------------------------------------- #
def test_bootstrap_creates_the_business_and_one_agent_per_spec_in_desk_order(conn, clock):
    business_id = bootstrap(conn, WARUNG_TEMPLATE, "  Warung Bu Tini  ", clock)

    business = conn.execute("SELECT id, name, type, paused FROM business").fetchall()
    assert [tuple(r) for r in business] == [(business_id, "Warung Bu Tini", "warung", 0)]  # name trimmed
    agents = conn.execute(
        "SELECT a.name, a.role, a.desk_index, a.paused, l.label FROM agent a "
        "JOIN agent_label l ON l.agent_id = a.id WHERE a.business_id = ? ORDER BY a.desk_index", (business_id,)
    ).fetchall()
    assert [tuple(r) for r in agents] == [
        ("Sari", "cashier", 0, 0, "Kasir"),
        ("Budi", "stock", 1, 0, "Stok"),
        ("Wati", "finance", 2, 0, "Keuangan"),
        ("Joko", "marketing", 3, 0, "Marketing"),
    ]


def test_warung_roles_map_to_the_four_agents(conn, clock):
    business_id = bootstrap(conn, WARUNG_TEMPLATE, "Warung Bu Tini", clock)
    roles = WARUNG_TEMPLATE.build_roles(conn, business_id, clock)
    ids = [r["id"] for r in conn.execute("SELECT id FROM agent ORDER BY desk_index")]
    assert [type(roles[i]) for i in ids] == [CashierRole, StockRole, FinanceRole, MarketingRole]
    assert all(roles[i].agent_id == i for i in ids)


def test_bootstrap_loads_the_demo_data(conn, clock):
    business_id = bootstrap(conn, WARUNG_TEMPLATE, "Warung Bu Tini", clock)
    today = date.fromisoformat(clock.today())

    products = data.list_products(conn, business_id)
    assert len(products) == len(CATALOG) == 12
    assert all(1_000 <= p["unit_price_idr"] <= 100_000 and p["unit_cost_idr"] < p["unit_price_idr"] for p in products)
    assert all(p["qty"] >= 0 for p in products)
    assert 1 <= len(data.low_stock(conn, business_id)) < len(products)  # some, not all, below the minimum

    history_days = [today - timedelta(days=n) for n in range(HISTORY_DAYS, 0, -1)]
    totals = data.sales_by_day(conn, business_id, history_days, clock)
    assert len(totals) == 7 and all(v > 0 for v in totals.values())
    last3 = list(totals.values())[-3:]
    assert last3[0] > last3[1] > last3[2], "sales must decline over the last 3 complete days"
    assert data.sales_total_for_day(conn, business_id, today) == 0  # nothing sold today yet

    # The sale totals are exact: each sale equals its items minus discounts.
    bad = conn.execute(
        "SELECT s.id FROM sale s JOIN sale_item i ON i.sale_id = s.id GROUP BY s.id "
        "HAVING s.total_idr != SUM(i.qty * i.unit_price_idr - i.discount_idr)"
    ).fetchall()
    assert bad == []


def test_seed_history_is_deterministic(clock):
    today = date.fromisoformat(clock.today())
    assert plan_history(today) == plan_history(today)


def test_bootstrap_does_not_set_the_policy(conn, clock):
    business_id = bootstrap(conn, WARUNG_TEMPLATE, "Warung Bu Tini", clock)
    assert conn.execute("SELECT COUNT(*) FROM policy").fetchone()[0] == 0
    assert WARUNG_TEMPLATE.default_policy is not None  # only a suggestion for the form
    assert conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0
    assert business_id > 0


def test_a_spend_before_the_policy_is_set_is_rejected_and_works_after(conn, clock):
    business_id = bootstrap(conn, WARUNG_TEMPLATE, "Warung Bu Tini", clock)
    agent_id = conn.execute("SELECT id FROM agent WHERE role = 'stock'").fetchone()[0]
    spend = Task(agent_id=agent_id, kind=TaskKind.SPEND, payload={}, cost_idr=10_000, reason="Beli gula")

    decision = authorize(conn, business_id, spend, clock)
    assert not decision.allowed and decision.reason == "policy not set"
    assert conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] == 0

    set_policy(conn, business_id, 100_000, [])
    assert authorize(conn, business_id, spend, clock).allowed
    assert conn.execute("SELECT SUM(amount_idr) FROM spend_ledger").fetchone()[0] == 10_000


def test_second_bootstrap_is_refused_and_writes_nothing(conn, clock):
    bootstrap(conn, toy_template(), "Toko Satu", clock)
    before = counts(conn)
    with pytest.raises(AlreadyOnboarded):
        bootstrap(conn, toy_template(), "Toko Dua", clock)
    with pytest.raises(AlreadyOnboarded):
        bootstrap(conn, WARUNG_TEMPLATE, "Warung", clock)
    assert counts(conn) == before


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\t\n", "x" * 61, " " + "x" * 61 + " ", "a\x00b", "a\nb", "a\u202eb", None, 7, b"Toko", ["Toko"]],
)
def test_invalid_names_are_rejected_before_anything_is_written(conn, clock, bad):
    before = counts(conn)
    with pytest.raises(InvalidBusinessName):
        bootstrap(conn, toy_template(), bad, clock)
    assert counts(conn) == before
    assert all(v == 0 for v in before.values())


@pytest.mark.parametrize(
    "good, expected",
    [("a", "a"), ("x" * 60, "x" * 60), ("  Toko Maju  ", "Toko Maju"), ("Warung \u2615 Bu Tini", "Warung \u2615 Bu Tini"),
     ("é" * 60, "é" * 60)],
)
def test_valid_names_are_trimmed_and_kept(good, expected):
    assert normalize_business_name(good) == expected


def test_a_failing_seed_leaves_nothing_behind_and_the_owner_can_retry(conn, clock):
    def broken_seed(c, business_id, clk):
        with db.transaction(c):
            c.execute(
                "INSERT INTO product (business_id, name, qty, min_qty, unit_price_idr) VALUES (?, 'Beras', 3, 1, 5000)",
                (business_id,),
            )
        raise RuntimeError("seed broke")

    with pytest.raises(RuntimeError, match="seed broke"):
        bootstrap(conn, toy_template(broken_seed), "Toko Gagal", clock)
    assert not conn.in_transaction
    assert all(v == 0 for v in counts(conn).values())

    business_id = bootstrap(conn, toy_template(), "Toko Berhasil", clock)  # a retry works
    assert conn.execute("SELECT name FROM business WHERE id = ?", (business_id,)).fetchone()[0] == "Toko Berhasil"
    assert counts(conn)["agent"] == 2 and counts(conn)["product"] == 1


def test_agents_and_business_are_created_in_one_transaction(conn, clock):
    """If creating the second agent fails, the business and the first agent are rolled back."""
    template = toy_template()
    conn.execute("CREATE TRIGGER stop_second_agent BEFORE INSERT ON agent WHEN NEW.desk_index = 1 "
                 "BEGIN SELECT RAISE(ABORT, 'no second agent'); END")
    with pytest.raises(Exception, match="no second agent"):
        bootstrap(conn, template, "Toko", clock)
    assert all(v == 0 for v in counts(conn).values())
    assert not conn.in_transaction


def test_seed_receives_connection_business_id_and_clock(conn, clock):
    seen = []
    business_id = bootstrap(conn, toy_template(lambda c, b, k: seen.append((c, b, k))), "Toko", clock)
    assert seen == [(conn, business_id, clock)]


# ---------------------------------------------------------------------- #
# Templates and registry
# ---------------------------------------------------------------------- #
def test_registry_lists_types_in_order_and_hides_functions():
    registry = TemplateRegistry([toy_template(key="toko"), toy_template(key="jasa")])
    assert registry.keys() == ["toko", "jasa"] and len(registry) == 2 and "jasa" in registry
    public = registry.public_list()
    assert public[0] == {
        "key": "toko", "label": "Toko", "description": "Toko contoh",
        "agents": [{"name": "Ani", "role": "Kasir"}, {"name": "Bima", "role": "Gudang"}],
    }
    json.dumps(public)  # plain data only


def test_registry_rejects_duplicates_and_unknown_keys():
    registry = TemplateRegistry([toy_template()])
    with pytest.raises(ValueError):
        registry.register(toy_template())
    for key in ("nope", "", None, 3, ["toko"]):
        with pytest.raises(UnknownBusinessType):
            registry.get(key)


@pytest.mark.parametrize("bad_key", ["", "Toko", "has space", "1abc", "x" * 33, None])
def test_template_rejects_a_bad_key(bad_key):
    with pytest.raises(ValueError):
        BusinessTemplate(key=bad_key, label="L", description="D", agents=(AgentSpec("a", "A", "R"),),
                         seed=lambda *a: None, build_roles=lambda *a: {})


def test_template_needs_agents_and_callables():
    with pytest.raises(ValueError):
        BusinessTemplate(key="x", label="L", description="D", agents=(), seed=lambda *a: None, build_roles=lambda *a: {})
    with pytest.raises(ValueError):
        BusinessTemplate(key="x", label="L", description="D", agents=(AgentSpec("a", "A", "R"),),
                         seed=None, build_roles=lambda *a: {})
    with pytest.raises(ValueError):
        AgentSpec("", "Ani", "Kasir")


def test_the_skeleton_onboarding_module_imports_nothing_from_apps():
    source = inspect.getsource(onboarding)
    assert "from apps" not in source and "import apps" not in source
