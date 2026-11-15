"""Property tests for stock integrity in the Warung data layer (Req 6.1, 11.1).

Self-contained: own helpers, no shared conftest.

The test keeps its own reference model of every product quantity and every
purchase order, drives random operation sequences through the real data layer
(in-memory SQLite), and compares the database with the model after every step.
"""
import sqlite3
from datetime import datetime, timezone

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from apps.kantor_warung import data
from apps.kantor_warung.data import DataError, PurchaseError, StockError
from skeleton.core import db
from skeleton.core.clock import FakeClock

OWN = 1  # the business under test
OTHER = 2  # second business, used for cross-business attempts
UNKNOWN_ID = 9999
TABLES = ("product", "sale", "sale_item", "purchase_order")


# ------------------------------------------------------------------ helpers --


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung A', 'warung')")
    conn.execute("INSERT INTO business (id, name, type) VALUES (2, 'Warung B', 'warung')")
    return conn


def _snapshot(conn):
    """Full contents of every table the data layer may write to."""
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in TABLES
    }


def _snapshot_stock(snapshot):
    """product_id -> qty from a snapshot (columns: id, business_id, name, qty, ...)."""
    return {row[0]: row[3] for row in snapshot["product"]}


def _db_stock(conn):
    return {row["id"]: row["qty"] for row in conn.execute("SELECT id, qty FROM product")}


def _db_orders(conn):
    return {
        row["id"]: (row["product_id"], row["qty"], row["status"])
        for row in conn.execute("SELECT id, product_id, qty, status FROM purchase_order")
    }


class _Model:
    """Independent reference model: stock per product and purchase orders."""

    def __init__(self):
        self.stock = {}  # product_id -> qty
        self.owner = {}  # product_id -> business_id
        self.price = {}  # product_id -> unit_price_idr
        self.orders = {}  # order_id -> {"product_id", "qty", "status", "business_id"}
        self.order_ids = []  # creation order, for picking an order by index


def _check_against_model(conn, model):
    """(a) no negative stock, plus: database and reference model agree."""
    rows = conn.execute("SELECT id, qty FROM product").fetchall()
    assert rows, "products must never disappear"
    for row in rows:
        assert row["qty"] >= 0, f"negative stock for product {row['id']}: {row['qty']}"
    assert _db_stock(conn) == model.stock
    assert _db_orders(conn) == {
        oid: (o["product_id"], o["qty"], o["status"]) for oid, o in model.orders.items()
    }


# -------------------------------------------------------- reference: sales --


def _predict_sale(model, business_id, items, discount_total):
    """What the sale must do, computed from the model only.

    Returns ``(malformed, unknown, short, bad_discount, wanted)``. The sale is
    accepted exactly when the four flags are all False. ``wanted`` is the sold
    quantity per product, merged over duplicate lines.
    """
    malformed = not _is_int(discount_total) or discount_total < 0
    for item in items:
        if not _is_int(item["product_id"]):
            malformed = True
        if not _is_int(item["qty"]) or item["qty"] < 1:
            malformed = True
        extra = item.get("discount_idr", 0)
        if not _is_int(extra) or extra < 0:
            malformed = True
    if malformed:
        return True, False, False, False, {}

    unknown = any(model.owner.get(i["product_id"]) != business_id for i in items)
    if unknown:
        return False, True, False, False, {}

    wanted = {}
    for item in items:
        wanted[item["product_id"]] = wanted.get(item["product_id"], 0) + item["qty"]
    short = any(qty > model.stock[pid] for pid, qty in wanted.items())

    bad_discount = False
    payable = 0
    for item in items:
        line_total = item["qty"] * model.price[item["product_id"]]
        line_discount = item.get("discount_idr", 0)
        if line_discount > line_total:
            bad_discount = True
        payable += line_total - line_discount
    if discount_total > payable:
        bad_discount = True
    return False, False, short, bad_discount, wanted


def _do_sale(conn, clock, model, business_id, items, discount_total):
    before = _snapshot(conn)
    malformed, unknown, short, bad_discount, wanted = _predict_sale(
        model, business_id, items, discount_total
    )
    must_reject = malformed or unknown or short or bad_discount

    try:
        sale_id = data.record_sale(conn, business_id, items, clock, discount_total)
    except DataError as exc:
        event("sale rejected")
        assert must_reject, f"valid sale was rejected: {exc!r}"
        # (b) a rejected sale changes nothing
        assert _snapshot(conn) == before
        # (e) a sale larger than the stock is rejected, never partially applied
        if short and not (malformed or unknown or bad_discount):
            event("sale rejected for stock")
            assert isinstance(exc, StockError), repr(exc)
        return

    event("sale accepted")
    assert not must_reject, "a sale that must be rejected was accepted"
    after = _snapshot(conn)
    # (c) each product drops by exactly the merged sold quantity, others untouched
    before_stock = _snapshot_stock(before)
    after_stock = _snapshot_stock(after)
    for pid, qty in before_stock.items():
        assert after_stock[pid] == qty - wanted.get(pid, 0), (pid, qty, wanted)
    assert len(after["sale"]) == len(before["sale"]) + 1
    assert after["sale"][-1][0] == sale_id
    assert len(after["sale_item"]) == len(before["sale_item"]) + len(items)
    assert len(after["purchase_order"]) == len(before["purchase_order"])
    for pid, qty in wanted.items():
        model.stock[pid] -= qty


# ------------------------------------------------ reference: purchase orders --


def _do_place(conn, model, business_id, product_id, qty, cost, status):
    before = _snapshot(conn)
    valid = (
        status in ("placed", "pending")
        and _is_int(qty)
        and qty >= 1
        and _is_int(cost)
        and cost >= 0
        and _is_int(product_id)
        and model.owner.get(product_id) == business_id
    )
    try:
        order_id = data.place_purchase_order(conn, business_id, product_id, qty, cost, status)
    except DataError as exc:
        event("order rejected")
        assert not valid, f"valid purchase order was rejected: {exc!r}"
        assert _snapshot(conn) == before
        return
    event("order placed")
    assert valid, "an invalid purchase order was accepted"
    assert order_id not in model.orders
    after = _snapshot(conn)
    # placing an order never touches stock
    assert after["product"] == before["product"]
    assert len(after["purchase_order"]) == len(before["purchase_order"]) + 1
    model.orders[order_id] = {
        "product_id": product_id,
        "qty": qty,
        "status": status,
        "business_id": business_id,
    }
    model.order_ids.append(order_id)


def _do_receive(conn, clock, model, business_id, order_id):
    before = _snapshot(conn)
    order = model.orders.get(order_id) if _is_int(order_id) else None
    receivable = (
        order is not None and order["business_id"] == business_id and order["status"] == "placed"
    )
    try:
        new_qty = data.receive_purchase(conn, business_id, order_id, clock)
    except DataError as exc:
        event("receipt rejected")
        assert not receivable, f"receivable order was rejected: {exc!r}"
        if _is_int(order_id):
            assert isinstance(exc, PurchaseError), repr(exc)
        assert _snapshot(conn) == before
        return

    event("receipt accepted")
    assert receivable, "an order that is not receivable was accepted"
    pid = order["product_id"]
    # (d) stock grows by exactly the order qty, once
    assert new_qty == model.stock[pid] + order["qty"]
    after = _snapshot(conn)
    before_stock = _snapshot_stock(before)
    after_stock = _snapshot_stock(after)
    for other_pid, qty in before_stock.items():
        expected = qty + order["qty"] if other_pid == pid else qty
        assert after_stock[other_pid] == expected, (other_pid, qty, order)
    model.stock[pid] += order["qty"]
    order["status"] = "received"

    # (d) receiving the same order a second time is rejected and changes nothing
    mid = _snapshot(conn)
    with pytest.raises(PurchaseError):
        data.receive_purchase(conn, business_id, order_id, clock)
    assert _snapshot(conn) == mid


# --------------------------------------------------------------- strategies --

_gap_seconds = st.integers(min_value=0, max_value=7200)
_ref = st.integers(min_value=0, max_value=1000)  # resolved against a pool at run time

def _weighted(*pairs):
    """one_of with integer weights: ``weight`` copies of each strategy.

    ``one_of`` drops duplicate strategy objects, so every copy is wrapped in its
    own ``map`` to stay distinct.
    """
    return st.one_of(
        *[strategy.map(lambda value: value) for strategy, weight in pairs for _ in range(weight)]
    )


_item_qty = _weighted(
    (st.integers(min_value=1, max_value=8), 24),
    (st.integers(min_value=9, max_value=120), 3),  # often larger than the stock
    (st.just(0), 1),
    (st.integers(min_value=-5, max_value=-1), 1),
    (st.sampled_from([1.5, "2", None, True, 2.0]), 1),
)
_item_discount = _weighted(
    (st.none(), 16),  # key omitted
    (st.just(0), 4),
    (st.integers(min_value=0, max_value=3000), 4),
    (st.integers(min_value=0, max_value=200_000), 1),  # can exceed the line total
    (st.integers(min_value=-5, max_value=-1), 1),
    (st.sampled_from([1.5, "1", True]), 1),
)
_sale_discount = _weighted(
    (st.just(0), 20),
    (st.integers(min_value=0, max_value=5_000), 3),
    (st.integers(min_value=0, max_value=500_000), 1),  # can exceed the sale total
    (st.just(-5), 1),
)
_sale_op = st.tuples(
    st.just("sale"),
    _gap_seconds,
    st.lists(st.tuples(_ref, _item_qty, _item_discount), min_size=1, max_size=4),
    _sale_discount,
)
_po_qty = _weighted(
    (st.integers(min_value=1, max_value=30), 20),
    (st.just(0), 1),
    (st.just(-3), 1),
    (st.sampled_from([1.5, "3", None]), 1),
)
_po_op = st.tuples(
    st.just("place"),
    _gap_seconds,
    st.sampled_from([OWN] * 15 + [OTHER]),
    _ref,
    _po_qty,
    _weighted((st.integers(min_value=0, max_value=100_000), 20), (st.just(-1), 1)),
    st.sampled_from(["placed"] * 12 + ["pending"] * 6 + ["bogus"]),
)
_recv_op = st.tuples(
    st.just("receive"),
    _gap_seconds,
    st.sampled_from([OWN] * 15 + [OTHER]),
    _weighted((_ref, 24), (st.just(None), 1)),  # None = malformed order id
)
_ops = st.lists(
    _weighted((_sale_op, 4), (_po_op, 3), (_recv_op, 3)),
    min_size=8,
    max_size=40,
)

_own_products = st.lists(
    st.tuples(st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=10_000)),
    min_size=1,
    max_size=5,
)
_other_products = st.lists(
    st.tuples(st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=10_000)),
    min_size=1,
    max_size=2,
)


# --------------------------------------------------------------------- tests --


@settings(max_examples=200, deadline=None)
@given(own=_own_products, other=_other_products, ops=_ops)
def test_stock_never_negative_over_random_operations(own, other, ops):
    # Property 1: Stock never negative.
    # For any sequence of sales and purchase receipts, every product.qty is
    # >= 0. A sale larger than the stock is rejected, not applied. A rejected
    # operation changes nothing; an accepted sale lowers each product by
    # exactly the sold quantity (duplicate lines merged); an accepted receipt
    # raises stock by exactly the order qty, once.
    # Validates: Requirements 6.1, 11.1
    conn = _make_conn()
    try:
        clock = FakeClock(datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc))
        model = _Model()
        own_ids, other_ids = [], []
        for business_id, products, ids in ((OWN, own, own_ids), (OTHER, other, other_ids)):
            for index, (qty, price) in enumerate(products):
                pid = data.add_product(
                    conn, business_id, f"P{business_id}-{index}", qty, 2, price, price // 2
                )
                ids.append(pid)
                model.stock[pid] = qty
                model.owner[pid] = business_id
                model.price[pid] = price
        # Own products are listed twelve times so most lines hit the business under test.
        pool = own_ids * 12 + other_ids + [UNKNOWN_ID, None, "1"]
        _check_against_model(conn, model)

        for op in ops:
            clock.advance(op[1])
            kind = op[0]
            if kind == "sale":
                _, _, lines, discount_total = op
                items = []
                for ref, qty, discount in lines:
                    item = {"product_id": pool[ref % len(pool)], "qty": qty}
                    if discount is not None:
                        item["discount_idr"] = discount
                    items.append(item)
                _do_sale(conn, clock, model, OWN, items, discount_total)
            elif kind == "place":
                _, _, business_id, ref, qty, cost, status = op
                _do_place(conn, model, business_id, pool[ref % len(pool)], qty, cost, status)
            else:
                _, _, business_id, ref = op
                # Any known order (placed, pending, received, other business),
                # occasionally an unknown id (every 16th ref) or a malformed one (None).
                if ref is None:
                    order_id = None
                elif ref % 16 == 0 or not model.order_ids:
                    order_id = UNKNOWN_ID
                else:
                    order_id = model.order_ids[ref % len(model.order_ids)]
                _do_receive(conn, clock, model, business_id, order_id)
            _check_against_model(conn, model)
    finally:
        conn.close()


def test_database_rejects_negative_stock_directly():
    # Property 1 (defense in depth): even a direct write cannot make stock
    # negative, because product.qty has CHECK (qty >= 0).
    # Validates: Requirements 6.1, 11.1
    conn = _make_conn()
    try:
        pid = data.add_product(conn, OWN, "Teh", 5, 1, 3000, 2000)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE product SET qty = -1 WHERE id = ?", (pid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE product SET qty = qty - 6 WHERE id = ?", (pid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO product (business_id, name, qty, unit_price_idr) VALUES (1, 'X', -1, 100)"
            )
        assert conn.execute("SELECT qty FROM product WHERE id = ?", (pid,)).fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1
    finally:
        conn.close()
