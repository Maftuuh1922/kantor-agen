"""Tests for the Kantor Warung roles (Req 1.2, 3.2, 3.3, 5.4, 6.3).

Real skeleton schema in memory SQLite, real Manager and Heartbeat, FakeClock and
FakeLLMClient. No network and no real waiting.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.kantor_warung import data
from apps.kantor_warung.roles import (
    WARUNG_ROLES,
    CashierRole,
    FinanceRole,
    MarketingRole,
    SimulatedSalesFeed,
    StockRole,
    build_roles,
    rupiah,
)
from skeleton.core import db
from skeleton.core.clock import JAKARTA, FakeClock
from skeleton.core.config import Settings
from skeleton.log.activity import recent
from skeleton.policy import set_policy
from skeleton.runtime import BusinessContext, FakeLLMClient, Heartbeat, Manager, RoleRegistry, TaskKind

CASHIER, STOCK, FINANCE, MARKETING = 1, 2, 3, 4
LIMIT = 100_000
TABLES = (
    "business", "agent", "policy", "spend_ledger", "product", "sale", "sale_item",
    "purchase_order", "activity", "outbox",
)


def jakarta(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=JAKARTA)


class ListFeed:
    """Feed that hands out whatever the test queued, once."""

    def __init__(self, *sales):
        self.queue = list(sales)

    def poll(self, ctx):
        sales, self.queue = self.queue, []
        return sales


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung Bu Tini', 'warung')")
    for agent_id, role in ((CASHIER, "cashier"), (STOCK, "stock"), (FINANCE, "finance"), (MARKETING, "marketing")):
        c.execute(
            "INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (?, 1, ?, ?, ?)",
            (agent_id, role, role.title(), agent_id - 1),
        )
    set_policy(c, 1, LIMIT, [])
    yield c
    c.close()


@pytest.fixture
def clock():
    # 2025-01-01 03:00 UTC = 10:00 Jakarta (a Wednesday)
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


def make_env(conn, clock, *, feed=None, finance_hour=17, llm=None):
    llm = llm or FakeLLMClient()
    settings = Settings(model_low="low", model_strong="strong")
    roles = build_roles(conn, 1, clock, feed=feed, finance_hour=finance_hour)
    manager = Manager(conn, 1, clock, llm, settings, roles)
    heartbeat = Heartbeat(conn, 1, clock, manager, roles, settings)
    return SimpleNamespace(roles=roles, llm=llm, heartbeat=heartbeat, ctx=BusinessContext(conn, 1, clock))


def add(conn, name, qty, min_qty=0, price=3000, cost=2000):
    return data.add_product(conn, 1, name, qty, min_qty, price, cost)


def qty_of(conn, product_id):
    return conn.execute("SELECT qty FROM product WHERE id = ?", (product_id,)).fetchone()[0]


def orders(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM purchase_order ORDER BY id")]


def ledger(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM spend_ledger ORDER BY id")]


def outbox(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def log(conn):
    return list(reversed(recent(conn, 1, limit=2000)))


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def by_status(results, status):
    return [r for r in results if r.status == status]


# ------------------------------------------------------------------ cashier --


async def test_cashier_records_sales_from_feed(conn, clock):
    teh = add(conn, "Teh Botol", 10)
    mie = add(conn, "Mie Instan", 10, price=3500)
    feed = ListFeed(
        {"items": [{"product_id": teh, "qty": 2}, {"product_id": mie, "qty": 1}]},
        {"items": [{"product_id": teh, "qty": 1}]},
    )
    env = make_env(conn, clock, feed=feed)

    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["done", "done"]
    assert results[0].message == "Terjual Rp 9.500 (2 produk)"
    assert qty_of(conn, teh) == 7 and qty_of(conn, mie) == 9
    assert conn.execute("SELECT COUNT(*) FROM sale").fetchone()[0] == 2
    reasons = [row["reason"] for row in log(conn) if row["agent_id"] == CASHIER]
    assert reasons[0] == "Catat penjualan: 2x Teh Botol, 1x Mie Instan"
    assert ledger(conn) == [] and outbox(conn) == []  # a sale moves no money and sends nothing
    assert len(env.llm.calls) == 0


async def test_cashier_failed_sale_is_a_failed_result_not_an_exception(conn, clock):
    teh = add(conn, "Teh Botol", 2)
    feed = ListFeed({"items": [{"product_id": teh, "qty": 5}]}, {"items": [{"product_id": 999, "qty": 1}]})
    env = make_env(conn, clock, feed=feed)

    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["failed", "failed"]
    assert "Teh Botol" in results[0].message and "stok" in results[0].message
    assert "tidak dikenal" in results[1].message
    assert qty_of(conn, teh) == 2
    assert conn.execute("SELECT COUNT(*) FROM sale").fetchone()[0] == 0
    assert [r["status"] for r in log(conn)] == ["failed", "failed"]


async def test_cashier_without_feed_does_nothing(conn, clock):
    add(conn, "Teh Botol", 10)
    env = make_env(conn, clock)
    assert await env.heartbeat.tick() == []


# -------------------------------------------------------------------- stock --


async def test_stock_orders_once_hands_off_to_finance_and_books_spend_there(conn, clock):
    teh = add(conn, "Teh Botol", 2, min_qty=10, cost=2000)  # needs 28 to reach 30 -> Rp 56.000
    env = make_env(conn, clock)

    results = await env.heartbeat.tick()

    done = by_status(results, "done")
    assert len(done) == 1 and done[0].cost_idr == 56_000
    assert orders(conn) == [
        {"id": 1, "product_id": teh, "qty": 28, "cost_idr": 56_000, "status": "placed", "received_at": None}
    ]
    assert [(row["agent_id"], row["amount_idr"]) for row in ledger(conn)] == [(FINANCE, 56_000)]
    handoffs = [row for row in log(conn) if row["action"] == "handoff"]
    assert len(handoffs) == 1 and handoffs[0]["agent_id"] == STOCK
    assert qty_of(conn, teh) == 2  # stock only grows on delivery

    # no double order, on this tick or the next ones
    for _ in range(3):
        clock.advance(300)
        await env.heartbeat.tick()
    assert len(orders(conn)) == 1 and len(ledger(conn)) == 1


async def test_stock_places_the_order_itself_without_an_active_finance_agent(conn, clock):
    add(conn, "Teh Botol", 2, min_qty=10)
    conn.execute("UPDATE agent SET paused = 1 WHERE id = ?", (FINANCE,))
    env = make_env(conn, clock)

    await env.heartbeat.tick()

    assert [row["agent_id"] for row in ledger(conn)] == [STOCK]
    assert orders(conn)[0]["status"] == "placed"


async def test_stock_goes_pending_when_cost_exceeds_remaining_limit(conn, clock):
    teh = add(conn, "Teh Botol", 2, min_qty=10, cost=2000)  # Rp 56.000
    set_policy(conn, 1, 20_000, [])
    env = make_env(conn, clock)

    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["pending"]
    assert "Menunggu pemilik" in results[0].message and len(results[0].message) <= 60
    assert [(o["product_id"], o["status"], o["cost_idr"]) for o in orders(conn)] == [(teh, "pending", 56_000)]
    assert ledger(conn) == []
    assert "limit" in [row for row in log(conn) if row["agent_id"] == STOCK][0]["reason"]

    # the order stays pending and is not repeated; it is never received on its own
    for _ in range(5):
        clock.advance(3600)
        await env.heartbeat.tick()
    assert [o["status"] for o in orders(conn)] == ["pending"]
    assert qty_of(conn, teh) == 2


async def test_stock_orders_in_one_tick_fit_the_limit_together(conn, clock):
    a = add(conn, "Gula", 0, min_qty=10, cost=1000)  # 30 pcs = Rp 30.000 fits
    b = add(conn, "Minyak", 0, min_qty=10, cost=2000)  # Rp 60.000 fits alone, not after Gula
    set_policy(conn, 1, 80_000, [])
    env = make_env(conn, clock)

    results = await env.heartbeat.tick()

    assert sorted(r.status for r in results) == ["done", "pending"]
    status = {o["product_id"]: o["status"] for o in orders(conn)}
    assert status == {a: "placed", b: "pending"}
    assert sum(row["amount_idr"] for row in ledger(conn)) == 30_000


async def test_stock_receives_delivery_after_lead_time(conn, clock):
    teh = add(conn, "Teh Botol", 2, min_qty=10)
    env = make_env(conn, clock)
    await env.heartbeat.tick()
    assert orders(conn)[0]["status"] == "placed"

    clock.advance(3600 + 3000)  # 1h50: still on its way
    await env.heartbeat.tick()
    assert orders(conn)[0]["status"] == "placed" and qty_of(conn, teh) == 2

    clock.advance(600)  # 2h exactly since the order
    results = await env.heartbeat.tick()
    assert orders(conn)[0]["status"] == "received" and orders(conn)[0]["received_at"] is not None
    assert qty_of(conn, teh) == 30
    assert [r.status for r in results] == ["done"] and "diterima" in results[0].message

    clock.advance(3600)
    await env.heartbeat.tick()  # stock is fine again: no new order, no second receipt
    assert len(orders(conn)) == 1 and qty_of(conn, teh) == 30


async def test_stock_reorders_after_delivery_if_still_low(conn, clock):
    teh = add(conn, "Teh Botol", 0, min_qty=10)
    env = make_env(conn, clock)
    await env.heartbeat.tick()
    clock.advance(7200)
    await env.heartbeat.tick()  # delivery of 30
    conn.execute("UPDATE product SET qty = 1 WHERE id = ?", (teh,))  # sold out again
    clock.advance(86400)  # next day: the daily limit is fresh
    await env.heartbeat.tick()
    assert [o["status"] for o in orders(conn)] == ["received", "placed"]


async def test_placed_order_without_marker_row_is_received(conn, clock):
    teh = add(conn, "Teh Botol", 1, min_qty=0)
    data.place_purchase_order(conn, 1, teh, 5, 10_000, "placed")  # e.g. a manual order
    env = make_env(conn, clock)
    await env.heartbeat.tick()
    assert qty_of(conn, teh) == 6


# ------------------------------------------------------------------ finance --


async def test_finance_summary_once_per_day_and_only_after_the_hour(conn, clock):
    teh = add(conn, "Teh Botol", 20)
    data.record_sale(conn, 1, [{"product_id": teh, "qty": 3}], clock)  # Rp 9.000 today
    llm = FakeLLMClient(["Hari ini lancar."])
    env = make_env(conn, clock, llm=llm)

    clock.set(jakarta(2025, 1, 1, 16, 59))
    assert await env.heartbeat.tick() == []
    assert len(llm.calls) == 0

    clock.set(jakarta(2025, 1, 1, 17, 0))
    results = await env.heartbeat.tick()
    assert [(r.status, r.message) for r in results] == [("done", "Hari ini lancar.")]  # model text wins
    assert len(llm.calls) == 1 and llm.calls[0]["model"] == "low"

    for hour in (17, 18, 23):
        clock.set(jakarta(2025, 1, 1, hour, 30))
        assert await env.heartbeat.tick() == []
    assert len(llm.calls) == 1

    clock.set(jakarta(2025, 1, 2, 17, 5))  # a new day
    assert len(await env.heartbeat.tick()) == 1


async def test_finance_summary_falls_back_to_rules_when_model_is_down(conn, clock):
    teh = add(conn, "Teh Botol", 20)
    add(conn, "Gula", 1, min_qty=5)
    data.record_sale(conn, 1, [{"product_id": teh, "qty": 3}], clock)
    llm = FakeLLMClient()
    llm.fail_next()
    env = make_env(conn, clock, llm=llm, finance_hour=10)  # clock is 10:00

    results = await env.heartbeat.tick()
    summary = [r for r in results if "Ringkasan" in r.message]

    assert len(summary) == 1 and summary[0].status == "done"
    text = summary[0].message
    assert "penjualan Rp 9.000 dari 1 transaksi" in text
    assert "Belanja stok Rp" in text and "sisa limit" in text
    assert "Gula" in text  # low stock mentioned
    assert summary[0].data["source"] == "rule"


async def test_finance_summary_mentions_pending_orders(conn, clock):
    add(conn, "Teh Botol", 2, min_qty=10)
    set_policy(conn, 1, 1000, [])
    llm = FakeLLMClient()
    llm.fail_next()  # model down: the rule-based summary is used
    env = make_env(conn, clock, finance_hour=0, llm=llm)
    results = await env.heartbeat.tick()
    summary = [r for r in results if r.status == "done"][0].message
    assert "Menunggu keputusan pemilik: Teh Botol (Rp 56.000)" in summary


# ---------------------------------------------------------------- marketing --


def sell_on(conn, clock, day, units, product_id):
    """Record `units` pieces sold at noon Jakarta of `day`, then restore the clock."""
    saved = clock.now()
    clock.set(jakarta(day.year, day.month, day.day, 12))
    if units:
        data.record_sale(conn, 1, [{"product_id": product_id, "qty": units}], clock)
    clock.set(saved)


@pytest.mark.parametrize(
    "units, fires",
    [
        ((30, 20, 10), True),
        ((5, 4, 0), True),
        ((30, 20, 20), False),  # not strictly declining
        ((30, 30, 10), False),
        ((10, 20, 30), False),
        ((30, 10, 20), False),
        ((0, 0, 0), False),
    ],
)
async def test_marketing_only_on_three_strictly_declining_days(conn, clock, units, fires):
    tea = add(conn, "Teh Botol", 1000)
    today = date.fromisoformat(clock.today())
    for back, n in zip((3, 2, 1), units):
        sell_on(conn, clock, today - timedelta(days=back), n, tea)
    env = make_env(conn, clock)

    tasks = env.roles[MARKETING].triggers(env.ctx)

    assert [t.kind for t in tasks] == ([TaskKind.DRAFT, TaskKind.CUSTOMER_MESSAGE] if fires else [])


async def test_marketing_ignores_today_incomplete_sales(conn, clock):
    tea = add(conn, "Teh Botol", 1000)
    today = date.fromisoformat(clock.today())
    for back, n in zip((3, 2, 1), (30, 20, 10)):
        sell_on(conn, clock, today - timedelta(days=back), n, tea)
    sell_on(conn, clock, today, 500, tea)  # a huge sale today must not matter
    assert len(make_env(conn, clock).roles[MARKETING].triggers(BusinessContext(conn, 1, clock))) == 2


def decline(conn, clock):
    tea = add(conn, "Teh Botol", 1000)
    add(conn, "Mie Instan", 40)
    today = date.fromisoformat(clock.today())
    for back, n in zip((3, 2, 1), (30, 20, 10)):
        sell_on(conn, clock, today - timedelta(days=back), n, tea)


async def test_marketing_puts_exactly_one_pending_message_in_the_outbox(conn, clock):
    decline(conn, clock)
    llm = FakeLLMClient(["Ide: beli 2 gratis 1.", "Halo pelanggan setia! Diskon 10% hari ini."])
    env = make_env(conn, clock, llm=llm)

    results = await env.heartbeat.tick()

    assert [r.status for r in results] == ["done", "done"]
    assert results[0].message == "Ide: beli 2 gratis 1."
    box = outbox(conn)
    assert len(box) == 1
    assert (box[0]["channel"], box[0]["recipient"], box[0]["status"], box[0]["agent_id"]) == (
        "whatsapp", "pelanggan setia", "pending", MARKETING,
    )
    assert box[0]["body"] == "Halo pelanggan setia! Diskon 10% hari ini."
    assert [c["model"] for c in llm.calls] == ["low", "strong"]

    for _ in range(5):  # never more than one promo per day
        clock.advance(1800)
        await env.heartbeat.tick()
    assert len(outbox(conn)) == 1 and len(llm.calls) == 2


async def test_marketing_blocked_message_is_not_retried_all_day(conn, clock):
    decline(conn, clock)
    set_policy(conn, 1, LIMIT, ["customer_message"])
    env = make_env(conn, clock)
    await env.heartbeat.tick()
    clock.advance(300)
    results = await env.heartbeat.tick()
    assert results == [] and outbox(conn) == []
    assert [r["status"] for r in log(conn) if r["action"] == "customer_message"] == ["rejected"]


async def test_marketing_template_body_is_used_when_requested(conn, clock):
    decline(conn, clock)
    env = make_env(conn, clock)
    env.roles[MARKETING].template_body = True
    await env.heartbeat.tick()
    body = outbox(conn)[0]["body"]
    assert "pelanggan setia" in body and "Warung Bu Tini" in body and "Teh Botol" in body


async def test_marketing_message_is_never_empty_when_model_is_down(conn, clock):
    decline(conn, clock)
    llm = FakeLLMClient()
    llm.fail_next(2)
    env = make_env(conn, clock, llm=llm)
    await env.heartbeat.tick()
    assert len(outbox(conn)) == 1 and outbox(conn)[0]["body"].strip()


# ------------------------------------------------------------- purity (3.3) --


async def test_triggers_are_pure(conn, clock):
    tea = add(conn, "Teh Botol", 1000)
    add(conn, "Gula", 0, min_qty=10)  # stock trigger
    old = add(conn, "Kopi", 5)
    data.place_purchase_order(conn, 1, old, 5, 10_000, "placed")  # delivery trigger
    today = date.fromisoformat(clock.today())
    for back, n in zip((3, 2, 1), (30, 20, 10)):  # marketing trigger
        sell_on(conn, clock, today - timedelta(days=back), n, tea)
    clock.set(jakarta(2025, 1, 1, 18, 0))  # finance trigger
    feed = ListFeed({"items": [{"product_id": tea, "qty": 1}]})  # cashier trigger
    env = make_env(conn, clock, feed=feed)
    before = counts(conn)
    products_before = [dict(r) for r in conn.execute("SELECT * FROM product ORDER BY id")]

    emitted = {agent_id: role.triggers(env.ctx) for agent_id, role in env.roles.items()}

    assert all(emitted.values())  # every role had something to do
    assert counts(conn) == before
    assert [dict(r) for r in conn.execute("SELECT * FROM product ORDER BY id")] == products_before
    assert len(env.llm.calls) == 0
    for agent_id, tasks in emitted.items():
        assert all(t.agent_id == agent_id and isinstance(t.cost_idr, int) for t in tasks)
    # triggers can be evaluated again with the same answer (nothing was consumed or written)
    again = {a: r.triggers(env.ctx) for a, r in env.roles.items() if a != CASHIER}
    assert {a: [t.reason for t in ts] for a, ts in again.items()} == {
        a: [t.reason for t in ts] for a, ts in emitted.items() if a != CASHIER
    }


# ------------------------------------------------------------ feed + wiring --


def test_simulated_feed_is_deterministic_and_bounded(conn, clock):
    products = [add(conn, f"Produk {i}", 50) for i in range(5)]
    ctx = BusinessContext(conn, 1, clock)
    a, b, other = SimulatedSalesFeed(1), SimulatedSalesFeed(1), SimulatedSalesFeed(2)
    seen_other = False
    total = 0
    for step in range(12 * 14):  # one open day in 5-minute slots
        clock.set(jakarta(2025, 1, 6, 7, 0) + timedelta(minutes=5 * step))
        sales = a.poll(ctx)
        assert sales == b.poll(ctx)
        seen_other |= sales != other.poll(ctx)
        assert 0 <= len(sales) <= 3
        for sale in sales:
            total += 1
            assert 1 <= len(sale["items"]) <= 3
            for item in sale["items"]:
                assert item["product_id"] in products and 1 <= item["qty"] <= 3
    assert total > 0 and seen_other
    clock.set(jakarta(2025, 1, 6, 3, 0))
    assert a.poll(ctx) == []  # shop closed at night


def test_simulated_feed_only_sells_what_is_in_stock(conn, clock):
    add(conn, "Kosong", 0)
    one = add(conn, "Satu", 1)
    ctx = BusinessContext(conn, 1, clock)
    feed = SimulatedSalesFeed(3)
    for step in range(12 * 14):
        clock.set(jakarta(2025, 1, 6, 7, 0) + timedelta(minutes=5 * step))
        sold = sum(i["qty"] for s in feed.poll(ctx) for i in s["items"])
        assert sold <= 1
        assert all(i["product_id"] == one for s in feed.poll(ctx) for i in s["items"])


def test_simulated_feed_sells_less_later_in_the_week(conn, clock):
    add(conn, "Banyak", 100_000)
    ctx = BusinessContext(conn, 1, clock)
    feed = SimulatedSalesFeed(5)

    def day_total(day):
        n = 0
        for step in range(12 * 14):
            clock.set(jakarta(2025, 1, day, 7, 0) + timedelta(minutes=5 * step))
            n += len(feed.poll(ctx))
        return n

    monday, tuesday, wednesday, thursday = (day_total(d) for d in (6, 7, 8, 9))
    assert monday > tuesday > wednesday > thursday


def test_build_roles_maps_agent_rows_by_role(conn, clock):
    conn.execute("INSERT INTO agent (id, business_id, role, name, desk_index) VALUES (9, 1, 'barista', 'X', 9)")
    feed = ListFeed()
    roles = build_roles(conn, 1, clock, feed=feed, finance_hour=20)

    assert sorted(roles) == [CASHIER, STOCK, FINANCE, MARKETING]
    assert isinstance(roles[CASHIER], CashierRole) and roles[CASHIER].feed is feed
    assert isinstance(roles[STOCK], StockRole)
    assert isinstance(roles[FINANCE], FinanceRole) and roles[FINANCE].summary_hour == 20
    assert isinstance(roles[MARKETING], MarketingRole)
    assert {a: r.agent_id for a, r in roles.items()} == {a: a for a in roles}
    assert [roles[a].name for a in sorted(roles)] == ["cashier", "stock", "finance", "marketing"]


def test_registry_has_the_four_roles_and_roles_find_their_agent_without_build_roles(conn, clock):
    assert isinstance(WARUNG_ROLES, RoleRegistry)
    assert WARUNG_ROLES.names() == ["cashier", "finance", "marketing", "stock"]
    add(conn, "Teh Botol", 1, min_qty=10)
    tasks = WARUNG_ROLES.create("stock").triggers(BusinessContext(conn, 1, clock))
    assert [t.agent_id for t in tasks] == [STOCK]


def test_rupiah_format():
    assert rupiah(0) == "Rp 0" and rupiah(1_250_000) == "Rp 1.250.000"


# ----------------------------------------------------------- 3-day full run --


async def test_three_day_simulation_stays_within_limit_and_stock_never_negative(conn, clock):
    for i, name in enumerate(["Teh Botol", "Mie Instan", "Kopi Sachet", "Gula", "Minyak", "Beras"]):
        add(conn, name, qty=12 + i, min_qty=8, price=4000 + 500 * i, cost=2500 + 300 * i)
    limit = 60_000
    set_policy(conn, 1, limit, [])
    env = make_env(conn, clock, feed=SimulatedSalesFeed(42))
    start = jakarta(2025, 1, 6, 0, 0)  # a Monday
    clock.set(start)

    for step in range(3 * 48):  # 3 days of 30 minute ticks
        clock.set(start + timedelta(minutes=30 * step))
        await env.heartbeat.tick()
        assert min(r[0] for r in conn.execute("SELECT qty FROM product")) >= 0

    per_day = conn.execute("SELECT day, SUM(amount_idr) FROM spend_ledger GROUP BY day").fetchall()
    assert per_day and all(total <= limit for _, total in per_day)
    assert conn.execute("SELECT COUNT(*) FROM sale").fetchone()[0] > 20  # the shop was alive
    assert any(o["status"] == "received" for o in orders(conn))  # restocked at least once
    assert not [row for row in log(conn) if row["status"] == "failed"]
    # one summary per day, and every spend was booked to the finance agent
    summaries = [r for r in log(conn) if r["action"] == "draft" and r["reason"].startswith("Ringkasan harian")]
    assert len(summaries) == 3
    assert {r[0] for r in conn.execute("SELECT DISTINCT agent_id FROM spend_ledger")} == {FINANCE}
    # no product is both on a placed order and ordered twice at once
    open_products = [o["product_id"] for o in orders(conn) if o["status"] in ("placed", "pending")]
    assert len(open_products) == len(set(open_products))
