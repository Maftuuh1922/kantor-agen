"""Example-based tests for the Warung data layer (Req 6.1, 6.2)."""

from datetime import date, datetime, timezone

import pytest

from apps.kantor_warung import data
from apps.kantor_warung.data import (
    DataError,
    PurchaseError,
    StockError,
    UnknownProductError,
)
from skeleton.core import db
from skeleton.core.clock import FakeClock


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung A', 'warung')")
    c.execute("INSERT INTO business (id, name, type) VALUES (2, 'Warung B', 'warung')")
    yield c
    c.close()


@pytest.fixture
def clock():
    # 2025-01-01 03:00 UTC = 10:00 in Jakarta
    return FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))


@pytest.fixture
def teh(conn):
    return data.add_product(conn, 1, "Teh", qty=10, min_qty=3, unit_price_idr=3000, unit_cost_idr=2000)


@pytest.fixture
def kopi(conn):
    return data.add_product(conn, 1, "Kopi", qty=5, min_qty=2, unit_price_idr=5000, unit_cost_idr=3500)


def _qty(conn, product_id):
    return conn.execute("SELECT qty FROM product WHERE id = ?", (product_id,)).fetchone()[0]


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("sale", "sale_item", "purchase_order")
    )


# ----------------------------------------------------------------- products --


def test_add_and_get_product(conn, teh):
    product = data.get_product(conn, 1, teh)
    assert product["name"] == "Teh"
    assert (product["qty"], product["min_qty"]) == (10, 3)
    assert (product["unit_price_idr"], product["unit_cost_idr"]) == (3000, 2000)


def test_get_product_is_scoped_to_business(conn, teh):
    assert data.get_product(conn, 2, teh) is None
    assert data.get_product(conn, 1, 999) is None


def test_list_products_only_own_business(conn, teh, kopi):
    other = data.add_product(conn, 2, "Gula", 1, 0, 100, 50)
    assert [p["id"] for p in data.list_products(conn, 1)] == [teh, kopi]
    assert [p["id"] for p in data.list_products(conn, 2)] == [other]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "   "},
        {"qty": -1},
        {"min_qty": -1},
        {"unit_price_idr": -1},
        {"unit_cost_idr": -1},
        {"qty": True},
        {"unit_price_idr": 10.5},
        {"min_qty": "3"},
    ],
)
def test_add_product_rejects_bad_input(conn, kwargs):
    args = dict(name="X", qty=1, min_qty=1, unit_price_idr=100, unit_cost_idr=50)
    args.update(kwargs)
    with pytest.raises(DataError):
        data.add_product(conn, 1, **args)
    assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 0


def test_low_stock_is_strictly_below_minimum(conn):
    below = data.add_product(conn, 1, "Below", qty=2, min_qty=3, unit_price_idr=1, unit_cost_idr=1)
    data.add_product(conn, 1, "Equal", qty=3, min_qty=3, unit_price_idr=1, unit_cost_idr=1)
    data.add_product(conn, 1, "Above", qty=4, min_qty=3, unit_price_idr=1, unit_cost_idr=1)
    data.add_product(conn, 2, "OtherBiz", qty=0, min_qty=5, unit_price_idr=1, unit_cost_idr=1)
    assert [p["id"] for p in data.low_stock(conn, 1)] == [below]


# -------------------------------------------------------------------- sales --


def test_record_sale_happy_path(conn, clock, teh, kopi):
    sale_id = data.record_sale(
        conn,
        1,
        [{"product_id": teh, "qty": 2}, {"product_id": kopi, "qty": 1, "discount_idr": 500}],
        clock,
    )
    sale = conn.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
    assert sale["business_id"] == 1
    assert sale["total_idr"] == 2 * 3000 + (5000 - 500)
    assert sale["created_at"] == "2025-01-01T03:00:00.000000+00:00"
    items = conn.execute("SELECT * FROM sale_item WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall()
    assert [(i["product_id"], i["qty"], i["unit_price_idr"], i["discount_idr"]) for i in items] == [
        (teh, 2, 3000, 0),
        (kopi, 1, 5000, 500),
    ]
    assert _qty(conn, teh) == 8
    assert _qty(conn, kopi) == 4


def test_sale_uses_price_at_sale_time(conn, clock, teh):
    first = data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock)
    conn.execute("UPDATE product SET unit_price_idr = 4000 WHERE id = ?", (teh,))
    second = data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock)
    totals = [conn.execute("SELECT total_idr FROM sale WHERE id = ?", (i,)).fetchone()[0] for i in (first, second)]
    assert totals == [3000, 4000]


def test_sale_can_sell_all_stock(conn, clock, teh):
    data.record_sale(conn, 1, [{"product_id": teh, "qty": 10}], clock)
    assert _qty(conn, teh) == 0


def test_sale_with_total_discount_is_spread_over_lines(conn, clock, teh, kopi):
    sale_id = data.record_sale(
        conn,
        1,
        [{"product_id": teh, "qty": 1, "discount_idr": 100}, {"product_id": kopi, "qty": 1}],
        clock,
        discount_idr_total=3000,
    )
    # Line 1 can still absorb 2900, the rest (100) goes to line 2.
    discounts = [r[0] for r in conn.execute("SELECT discount_idr FROM sale_item WHERE sale_id = ? ORDER BY id", (sale_id,))]
    assert discounts == [3000, 100]
    total = conn.execute("SELECT total_idr FROM sale WHERE id = ?", (sale_id,)).fetchone()[0]
    assert total == (3000 + 5000) - 3000 - 100


def test_total_discount_above_sale_total_is_rejected(conn, clock, teh):
    before = _counts(conn)
    with pytest.raises(DataError):
        data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock, discount_idr_total=3001)
    assert _counts(conn) == before
    assert _qty(conn, teh) == 10


def test_sale_may_be_free_with_full_discount(conn, clock, teh):
    sale_id = data.record_sale(conn, 1, [{"product_id": teh, "qty": 2, "discount_idr": 6000}], clock)
    assert conn.execute("SELECT total_idr FROM sale WHERE id = ?", (sale_id,)).fetchone()[0] == 0
    assert _qty(conn, teh) == 8


def test_item_discount_above_line_total_rejected(conn, clock, teh, kopi):
    before = _counts(conn)
    with pytest.raises(DataError, match="discount"):
        data.record_sale(
            conn,
            1,
            [{"product_id": kopi, "qty": 1}, {"product_id": teh, "qty": 2, "discount_idr": 6001}],
            clock,
        )
    assert _counts(conn) == before
    assert (_qty(conn, teh), _qty(conn, kopi)) == (10, 5)


def test_insufficient_stock_rejects_whole_sale(conn, clock, teh, kopi):
    before = _counts(conn)
    with pytest.raises(StockError):
        data.record_sale(
            conn, 1, [{"product_id": teh, "qty": 3}, {"product_id": kopi, "qty": 6}], clock
        )
    assert _counts(conn) == before == (0, 0, 0)
    assert (_qty(conn, teh), _qty(conn, kopi)) == (10, 5)
    assert not conn.in_transaction


def test_duplicate_lines_are_merged_for_stock_check(conn, clock, teh):
    before = _counts(conn)
    with pytest.raises(StockError):
        data.record_sale(
            conn, 1, [{"product_id": teh, "qty": 6}, {"product_id": teh, "qty": 5}], clock
        )
    assert _counts(conn) == before
    assert _qty(conn, teh) == 10


def test_duplicate_lines_within_stock_reduce_by_sum(conn, clock, teh):
    sale_id = data.record_sale(
        conn, 1, [{"product_id": teh, "qty": 6}, {"product_id": teh, "qty": 4}], clock
    )
    assert _qty(conn, teh) == 0
    assert conn.execute("SELECT COUNT(*) FROM sale_item WHERE sale_id = ?", (sale_id,)).fetchone()[0] == 2
    assert conn.execute("SELECT total_idr FROM sale WHERE id = ?", (sale_id,)).fetchone()[0] == 30000


def test_product_of_other_business_rejected(conn, clock, teh):
    foreign = data.add_product(conn, 2, "Gula", 50, 1, 1000, 500)
    before = _counts(conn)
    with pytest.raises(UnknownProductError):
        data.record_sale(
            conn, 1, [{"product_id": teh, "qty": 1}, {"product_id": foreign, "qty": 1}], clock
        )
    assert _counts(conn) == before
    assert (_qty(conn, teh), _qty(conn, foreign)) == (10, 50)


def test_unknown_product_rejected(conn, clock):
    with pytest.raises(UnknownProductError):
        data.record_sale(conn, 1, [{"product_id": 999, "qty": 1}], clock)


@pytest.mark.parametrize(
    "item",
    [
        {"qty": 1},
        {"product_id": "1", "qty": 1},
        {"product_id": 1, "qty": 0},
        {"product_id": 1, "qty": -2},
        {"product_id": 1, "qty": True},
        {"product_id": 1, "qty": 1.5},
        {"product_id": 1, "qty": 1, "discount_idr": -1},
        {"product_id": 1, "qty": 1, "discount_idr": 1.0},
        "not a dict",
    ],
)
def test_malformed_items_rejected(conn, clock, teh, item):
    assert teh == 1
    with pytest.raises(DataError):
        data.record_sale(conn, 1, [item], clock)
    assert _counts(conn) == (0, 0, 0)
    assert _qty(conn, teh) == 10


def test_empty_items_rejected(conn, clock):
    with pytest.raises(DataError):
        data.record_sale(conn, 1, [], clock)


def test_negative_total_discount_rejected(conn, clock, teh):
    with pytest.raises(DataError):
        data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock, discount_idr_total=-1)


# ---------------------------------------------------------- purchase orders --


def test_place_purchase_order_does_not_change_stock(conn, teh):
    placed = data.place_purchase_order(conn, 1, teh, qty=20, cost_idr=40000, status="placed")
    pending = data.place_purchase_order(conn, 1, teh, qty=5, cost_idr=10000, status="pending")
    rows = conn.execute("SELECT id, qty, cost_idr, status, received_at FROM purchase_order ORDER BY id").fetchall()
    assert [(r["id"], r["qty"], r["cost_idr"], r["status"], r["received_at"]) for r in rows] == [
        (placed, 20, 40000, "placed", None),
        (pending, 5, 10000, "pending", None),
    ]
    assert _qty(conn, teh) == 10


@pytest.mark.parametrize(
    "kwargs",
    [{"status": "received"}, {"status": "shipped"}, {"qty": 0}, {"qty": True}, {"cost_idr": -1}, {"cost_idr": 1.5}],
)
def test_place_purchase_order_rejects_bad_input(conn, teh, kwargs):
    args = dict(qty=1, cost_idr=100, status="placed")
    args.update(kwargs)
    with pytest.raises(DataError):
        data.place_purchase_order(conn, 1, teh, **args)
    assert _counts(conn)[2] == 0


def test_place_purchase_order_for_other_business_product(conn, teh):
    with pytest.raises(UnknownProductError):
        data.place_purchase_order(conn, 2, teh, 1, 100, "placed")
    assert _counts(conn)[2] == 0


def test_receive_purchase_adds_stock_and_marks_received(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 20, 40000, "placed")
    assert data.receive_purchase(conn, 1, po, clock) == 30
    assert _qty(conn, teh) == 30
    row = conn.execute("SELECT status, received_at FROM purchase_order WHERE id = ?", (po,)).fetchone()
    assert row["status"] == "received"
    assert row["received_at"] == "2025-01-01T03:00:00.000000+00:00"


def test_receive_purchase_defaults_to_system_clock(conn, teh):
    po = data.place_purchase_order(conn, 1, teh, 1, 100, "placed")
    data.receive_purchase(conn, 1, po)
    assert conn.execute("SELECT received_at FROM purchase_order").fetchone()[0] is not None


def test_second_receipt_is_rejected(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 20, 40000, "placed")
    data.receive_purchase(conn, 1, po, clock)
    with pytest.raises(PurchaseError):
        data.receive_purchase(conn, 1, po, clock)
    assert _qty(conn, teh) == 30


def test_pending_order_cannot_be_received(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 20, 40000, "pending")
    with pytest.raises(PurchaseError):
        data.receive_purchase(conn, 1, po, clock)
    assert _qty(conn, teh) == 10
    assert conn.execute("SELECT status FROM purchase_order").fetchone()[0] == "pending"


def test_receive_unknown_or_foreign_order_rejected(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 20, 40000, "placed")
    with pytest.raises(PurchaseError):
        data.receive_purchase(conn, 2, po, clock)
    with pytest.raises(PurchaseError):
        data.receive_purchase(conn, 1, 999, clock)
    assert _qty(conn, teh) == 10
    assert conn.execute("SELECT status FROM purchase_order").fetchone()[0] == "placed"


def _block_stock_updates(conn):
    """Make every later UPDATE of product fail, i.e. fail after earlier writes."""
    conn.execute(
        "CREATE TRIGGER block_stock BEFORE UPDATE ON product "
        "BEGIN SELECT RAISE(ABORT, 'stock update blocked'); END"
    )


def test_receive_purchase_is_atomic_when_stock_update_fails(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 20, 40000, "placed")
    _block_stock_updates(conn)
    with pytest.raises(Exception, match="stock update blocked"):
        data.receive_purchase(conn, 1, po, clock)
    row = conn.execute("SELECT status, received_at FROM purchase_order WHERE id = ?", (po,)).fetchone()
    assert (row["status"], row["received_at"]) == ("placed", None)
    assert _qty(conn, teh) == 10
    assert not conn.in_transaction


def test_record_sale_is_atomic_when_stock_update_fails(conn, clock, teh):
    _block_stock_updates(conn)
    with pytest.raises(Exception, match="stock update blocked"):
        data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock)
    assert _counts(conn) == (0, 0, 0)
    assert _qty(conn, teh) == 10
    assert not conn.in_transaction


def test_receive_then_sell_keeps_stock_consistent(conn, clock, teh):
    po = data.place_purchase_order(conn, 1, teh, 5, 10000, "placed")
    data.receive_purchase(conn, 1, po, clock)
    data.record_sale(conn, 1, [{"product_id": teh, "qty": 15}], clock)
    assert _qty(conn, teh) == 0
    with pytest.raises(StockError):
        data.record_sale(conn, 1, [{"product_id": teh, "qty": 1}], clock)
    assert _qty(conn, teh) == 0


# ------------------------------------------------------------- sales totals --


def _sell_at(conn, clock, when, product_id, qty=1):
    clock.set(when)
    return data.record_sale(conn, 1, [{"product_id": product_id, "qty": qty}], clock)


def test_sales_total_for_day_uses_jakarta_day(conn, clock, teh):
    # 16:59 UTC on Jan 1 = 23:59 Jakarta Jan 1; 17:00 UTC = 00:00 Jakarta Jan 2.
    _sell_at(conn, clock, datetime(2025, 1, 1, 16, 59, tzinfo=timezone.utc), teh)
    _sell_at(conn, clock, datetime(2025, 1, 1, 17, 0, tzinfo=timezone.utc), teh, qty=2)
    _sell_at(conn, clock, datetime(2024, 12, 31, 17, 0, tzinfo=timezone.utc), teh, qty=3)  # Jan 1 00:00 Jakarta
    assert data.sales_total_for_day(conn, 1, "2025-01-01") == (1 + 3) * 3000
    assert data.sales_total_for_day(conn, 1, date(2025, 1, 2)) == 2 * 3000
    assert data.sales_total_for_day(conn, 1, "2025-01-03") == 0


def test_sales_total_for_day_is_per_business(conn, clock, teh):
    other = data.add_product(conn, 2, "Gula", 5, 0, 1000, 500)
    _sell_at(conn, clock, datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc), teh)
    data.record_sale(conn, 2, [{"product_id": other, "qty": 2}], clock)
    assert data.sales_total_for_day(conn, 1, "2025-01-01") == 3000
    assert data.sales_total_for_day(conn, 2, "2025-01-01") == 2000


def test_sales_total_for_day_rejects_bad_day(conn):
    for bad in ("01-01-2025", "", None, datetime(2025, 1, 1)):
        with pytest.raises(DataError):
            data.sales_total_for_day(conn, 1, bad)


def test_sales_by_day_last_n_days(conn, clock, teh):
    _sell_at(conn, clock, datetime(2024, 12, 30, 3, 0, tzinfo=timezone.utc), teh)
    _sell_at(conn, clock, datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc), teh, qty=2)
    result = data.sales_by_day(conn, 1, 3, clock)
    assert list(result.items()) == [("2024-12-30", 3000), ("2024-12-31", 0), ("2025-01-01", 6000)]


def test_sales_by_day_explicit_days(conn, clock, teh):
    _sell_at(conn, clock, datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc), teh)
    assert data.sales_by_day(conn, 1, ["2025-01-01", date(2025, 1, 2)]) == {
        "2025-01-01": 3000,
        "2025-01-02": 0,
    }


def test_sales_by_day_rejects_bad_count(conn):
    for bad in (0, -1, True, 2.5):
        with pytest.raises(DataError):
            data.sales_by_day(conn, 1, bad)
