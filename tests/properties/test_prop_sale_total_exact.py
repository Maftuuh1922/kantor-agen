"""Property tests for exact sale totals (Req 6.2, 11.1).

Self-contained: own helpers, no shared conftest.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from apps.kantor_warung import data
from apps.kantor_warung.data import DataError
from skeleton.core import db
from skeleton.core.clock import FakeClock

BUSINESS_ID = 1
JAKARTA_OFFSET = timedelta(hours=7)  # Asia/Jakarta has no DST
EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)
# Wide enough to hold every Jakarta day a generated sale can land on.
QUERY_DAYS = [date(2024, 12, 31) + timedelta(days=i) for i in range(11)]  # 12-31 .. 01-10

# Offsets from the UTC midnight of the chosen day. 17:00 UTC is Jakarta midnight.
_BOUNDARY_OFFSETS = [
    timedelta(0),
    timedelta(hours=16, minutes=59, seconds=59),
    timedelta(hours=16, minutes=59, seconds=59, microseconds=999999),
    timedelta(hours=17),
    timedelta(hours=17, microseconds=1),
    timedelta(hours=23, minutes=59, seconds=59, microseconds=999999),
]


# ---------------------------------------------------------------- strategies --


def _time_of_day():
    random_offset = st.builds(
        lambda s, us: timedelta(seconds=s, microseconds=us),
        st.integers(0, 86_399),
        st.integers(0, 999_999),
    )
    return st.one_of(st.sampled_from(_BOUNDARY_OFFSETS), random_offset)


def _timestamp():
    return st.builds(
        lambda d, offset: EPOCH + timedelta(days=d) + offset,
        st.integers(0, 7),
        _time_of_day(),
    )


@st.composite
def _lines(draw, prices):
    """1 to 6 valid lines: ``(product index, qty, line discount)``, repeats allowed."""
    count = draw(st.integers(1, 6))
    lines = []
    for _ in range(count):
        index = draw(st.integers(0, len(prices) - 1))
        qty = draw(st.integers(1, 20))
        line_total = qty * prices[index]
        discount = draw(
            st.one_of(st.just(0), st.just(line_total), st.integers(0, line_total))
        )
        lines.append((index, qty, discount))
    return lines


def _payable(lines, prices):
    return sum(qty * prices[i] - disc for i, qty, disc in lines)


@st.composite
def _scenarios(draw):
    prices = draw(st.lists(st.integers(0, 500_000), min_size=1, max_size=6))
    sales = []
    for _ in range(draw(st.integers(1, 5))):
        lines = draw(_lines(prices))
        sale_discount = draw(
            st.one_of(
                st.just(0),
                st.just(_payable(lines, prices)),
                st.integers(0, _payable(lines, prices)),
            )
        )
        sales.append((draw(_timestamp()), lines, sale_discount))
    sold = [0] * len(prices)
    for _, lines, _ in sales:
        for i, qty, _ in lines:
            sold[i] += qty
    stock = [s + draw(st.sampled_from([0, 0, 1, 5, 10])) for s in sold]
    new_prices = draw(st.lists(st.integers(0, 500_000), min_size=len(prices), max_size=len(prices)))
    return prices, stock, sales, new_prices


# ------------------------------------------------------------------- helpers --


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO business (id, name, type) VALUES (1, 'Warung', 'warung')")
    return conn


def _add_products(conn, prices, stock):
    return [
        data.add_product(conn, BUSINESS_ID, f"P{i}", stock[i], 0, price, 0)
        for i, price in enumerate(prices)
    ]


def _item_dicts(product_ids, lines):
    return [
        {"product_id": product_ids[i], "qty": qty, "discount_idr": disc}
        for i, qty, disc in lines
    ]


def _stock(conn, product_ids):
    return [data.get_product(conn, BUSINESS_ID, pid)["qty"] for pid in product_ids]


def _sale_row(conn, sale_id):
    return conn.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()


def _item_rows(conn, sale_id):
    return conn.execute(
        "SELECT product_id, qty, unit_price_idr, discount_idr FROM sale_item "
        "WHERE sale_id = ? ORDER BY id",
        (sale_id,),
    ).fetchall()


def _snapshot(conn):
    """Everything stored about sales, as plain tuples, for before/after comparison."""
    sales = conn.execute("SELECT id, created_at, total_idr FROM sale ORDER BY id").fetchall()
    return [
        (tuple(s), [tuple(r) for r in _item_rows(conn, s["id"])]) for s in sales
    ]


def _jakarta_day(moment):
    return (moment + JAKARTA_OFFSET).date()


def _row_counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("sale", "sale_item")
    )


# ----------------------------------------------------------------- properties --


@settings(max_examples=200, deadline=None)
@given(scenario=_scenarios())
def test_sale_total_is_exact(scenario):
    # Property 2: Sale total is exact.
    # For any set of items, sale.total_idr equals the sum of
    # qty * unit_price_idr - discount_idr over its items, and each product's
    # stock drops by exactly the sold quantity. Per-day totals add up to the
    # sum of all sale totals, and later price changes do not alter old sales.
    # Validates: Requirements 6.2, 11.1
    prices, stock, sales, new_prices = scenario
    conn = _make_conn()
    try:
        product_ids = _add_products(conn, prices, stock)
        clock = FakeClock(EPOCH)
        expected_stock = list(stock)
        expected_per_day: dict[date, int] = {}
        expected_totals = []

        for moment, lines, sale_discount in sales:
            clock.set(moment)
            sale_id = data.record_sale(
                conn, BUSINESS_ID, _item_dicts(product_ids, lines), clock, sale_discount
            )

            # Expected total, computed only from the generated input.
            expected_total = _payable(lines, prices) - sale_discount
            sale = _sale_row(conn, sale_id)
            items = _item_rows(conn, sale_id)

            # (c) non-negative integer
            assert isinstance(sale["total_idr"], int) and not isinstance(sale["total_idr"], bool)
            assert sale["total_idr"] >= 0

            # (a) total equals the input-derived total and the stored-items total
            assert sale["total_idr"] == expected_total, (lines, sale_discount, prices)
            assert sale["total_idr"] == sum(
                r["qty"] * r["unit_price_idr"] - r["discount_idr"] for r in items
            )

            # (e) one item row per line, with the price at sale time
            assert len(items) == len(lines)
            for row, (i, qty, disc) in zip(items, lines):
                line_total = qty * prices[i]
                assert row["product_id"] == product_ids[i]
                assert row["qty"] == qty
                assert row["unit_price_idr"] == prices[i]
                assert disc <= row["discount_idr"] <= line_total
                if sale_discount == 0:
                    assert row["discount_idr"] == disc  # discount stored as given
            # the sale-level discount is spread over the lines without loss
            assert sum(r["discount_idr"] for r in items) == (
                sum(d for _, _, d in lines) + sale_discount
            )

            # (b) stock dropped by exactly the sold quantity, merged over lines
            for i, qty, _ in lines:
                expected_stock[i] -= qty
            assert _stock(conn, product_ids) == expected_stock

            # (d) bookkeeping for the per-day check, on the Jakarta day
            day = _jakarta_day(moment)
            expected_per_day[day] = expected_per_day.get(day, 0) + expected_total
            expected_totals.append(expected_total)

        def check_totals():
            by_day = data.sales_by_day(conn, BUSINESS_ID, QUERY_DAYS)
            assert sum(by_day.values()) == sum(expected_totals)
            assert sum(
                r["total_idr"] for r in conn.execute("SELECT total_idr FROM sale")
            ) == sum(expected_totals)
            for day in QUERY_DAYS:
                want = expected_per_day.get(day, 0)
                assert by_day[day.isoformat()] == want, (day, moment_list)
                assert data.sales_total_for_day(conn, BUSINESS_ID, day) == want
                assert data.sales_total_for_day(conn, BUSINESS_ID, day.isoformat()) == want
            # the "last N days" form agrees with the explicit list
            clock.set(datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc))
            assert data.sales_by_day(conn, BUSINESS_ID, len(QUERY_DAYS), clock) == by_day

        moment_list = [m.isoformat() for m, _, _ in sales]
        check_totals()

        # Prices are read at sale time: changing them later changes nothing stored.
        before = _snapshot(conn)
        for pid, price in zip(product_ids, new_prices):
            conn.execute("UPDATE product SET unit_price_idr = ? WHERE id = ?", (price, pid))
        assert _snapshot(conn) == before
        check_totals()
    finally:
        conn.close()


_BAD_KINDS = [
    "line_discount_above_total",
    "line_discount_negative",
    "line_discount_float",
    "line_discount_bool",
    "qty_zero",
    "qty_negative",
    "qty_float",
    "qty_bool",
    "sale_discount_above_total",
    "sale_discount_negative",
    "sale_discount_float",
    "sale_discount_bool",
]


@settings(max_examples=200, deadline=None)
@given(
    prices=st.lists(st.integers(0, 500_000), min_size=1, max_size=6),
    data_=st.data(),
    kind=st.sampled_from(_BAD_KINDS),
    extra=st.integers(0, 1000),
)
def test_invalid_sales_are_rejected_without_side_effects(prices, data_, kind, extra):
    # Property 2 (rejection side): an invalid sale (discount above what it
    # applies to, negative/float/bool money, zero or non-int qty) raises and
    # leaves no sale row, no sale_item row, and no stock change.
    # Validates: Requirements 6.2, 11.1
    lines = data_.draw(_lines(prices))
    stock = [sum(q for i, q, _ in lines if i == p) + 3 for p in range(len(prices))]
    k = data_.draw(st.integers(0, len(lines) - 1))

    items_in = [list(line) for line in lines]
    sale_discount = 0
    index, qty, _ = lines[k]
    line_total = qty * prices[index]
    if kind == "line_discount_above_total":
        items_in[k][2] = line_total + 1 + extra
    elif kind == "line_discount_negative":
        items_in[k][2] = -(1 + extra)
    elif kind == "line_discount_float":
        items_in[k][2] = float(extra)
    elif kind == "line_discount_bool":
        items_in[k][2] = bool(extra % 2)
    elif kind == "qty_zero":
        items_in[k][1] = 0
    elif kind == "qty_negative":
        items_in[k][1] = -(1 + extra)
    elif kind == "qty_float":
        items_in[k][1] = float(1 + extra)
    elif kind == "qty_bool":
        items_in[k][1] = True
    elif kind == "sale_discount_above_total":
        sale_discount = _payable(lines, prices) + 1 + extra
    elif kind == "sale_discount_negative":
        sale_discount = -(1 + extra)
    elif kind == "sale_discount_float":
        sale_discount = float(extra)
    else:  # sale_discount_bool
        sale_discount = bool(extra % 2)

    conn = _make_conn()
    try:
        product_ids = _add_products(conn, prices, stock)
        items = _item_dicts(product_ids, [tuple(x) for x in items_in])
        products_before = data.list_products(conn, BUSINESS_ID)

        with pytest.raises(DataError):
            data.record_sale(conn, BUSINESS_ID, items, FakeClock(EPOCH), sale_discount)

        assert _row_counts(conn) == (0, 0)
        assert data.list_products(conn, BUSINESS_ID) == products_before
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["qty", "min_qty", "unit_price_idr", "unit_cost_idr"])
@pytest.mark.parametrize("bad", [-1, 1.0, 2.5, True, False, "100", None])
def test_add_product_rejects_bad_money_and_counts(field, bad):
    # Property 2 (inputs): negative, float, bool or string numbers are rejected
    # when a product is created, so prices used in sales are always valid ints.
    # Validates: Requirements 6.2, 11.1
    conn = _make_conn()
    try:
        values = {"qty": 1, "min_qty": 0, "unit_price_idr": 1000, "unit_cost_idr": 500}
        values[field] = bad
        with pytest.raises(DataError):
            data.add_product(conn, BUSINESS_ID, "Teh", **values)
        assert data.list_products(conn, BUSINESS_ID) == []
    finally:
        conn.close()
