"""Warung data layer: products, sales, purchase orders, sales totals.

Money is integer rupiah. Stock (``product.qty``) changes only through
:func:`record_sale` (decrease) and :func:`receive_purchase` (increase), each in
the same transaction as the sale or purchase row (Req 6.1, 6.2). Every write
function is all-or-nothing: if it raises, nothing was written.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence

from skeleton.core.clock import JAKARTA, Clock, SystemClock
from skeleton.core.db import transaction

PURCHASE_OPEN_STATUSES = frozenset({"placed", "pending"})


class DataError(ValueError):
    """Base class for every rejection raised by this module."""


class StockError(DataError):
    """A sale would make a product's stock negative."""


class UnknownProductError(DataError):
    """The product does not exist or belongs to another business."""


class PurchaseError(DataError):
    """The purchase order does not exist, is not receivable, or was received."""


# --------------------------------------------------------------- validation --


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(name: str, value: Any, *, minimum: int = 0) -> int:
    if not _is_int(value) or value < minimum:
        kind = "a positive int" if minimum > 0 else "a non-negative int"
        raise DataError(f"{name} must be {kind}")
    return value


def _iso(moment: datetime) -> str:
    # Same fixed format as skeleton/log/activity.py, so string order is time order.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _product_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


# ----------------------------------------------------------------- products --


def add_product(
    conn: sqlite3.Connection,
    business_id: int,
    name: str,
    qty: int,
    min_qty: int,
    unit_price_idr: int,
    unit_cost_idr: int,
) -> int:
    """Insert a product and return its id."""
    if not isinstance(name, str) or not name.strip():
        raise DataError("name must be a non-empty string")
    _require_int("qty", qty)
    _require_int("min_qty", min_qty)
    _require_int("unit_price_idr", unit_price_idr)
    _require_int("unit_cost_idr", unit_cost_idr)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO product (business_id, name, qty, min_qty, unit_price_idr, unit_cost_idr) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (business_id, name.strip(), qty, min_qty, unit_price_idr, unit_cost_idr),
        )
        return int(cur.lastrowid)


def get_product(conn: sqlite3.Connection, business_id: int, product_id: int) -> dict | None:
    """Return one product of this business, or ``None``."""
    row = conn.execute(
        "SELECT * FROM product WHERE id = ? AND business_id = ?", (product_id, business_id)
    ).fetchone()
    return _product_dict(row) if row else None


def list_products(conn: sqlite3.Connection, business_id: int) -> list[dict]:
    """All products of this business, ordered by id."""
    rows = conn.execute("SELECT * FROM product WHERE business_id = ? ORDER BY id", (business_id,))
    return [_product_dict(r) for r in rows]


def low_stock(conn: sqlite3.Connection, business_id: int) -> list[dict]:
    """Products whose stock is below their minimum (``qty < min_qty``)."""
    rows = conn.execute(
        "SELECT * FROM product WHERE business_id = ? AND qty < min_qty ORDER BY id", (business_id,)
    )
    return [_product_dict(r) for r in rows]


# -------------------------------------------------------------------- sales --


def _validate_items(items: Sequence[dict]) -> list[tuple[int, int, int]]:
    """Return ``(product_id, qty, discount_idr)`` per line, or raise."""
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items:
        raise DataError("items must be a non-empty list")
    lines = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise DataError(f"item {index} must be a dict")
        product_id = item.get("product_id")
        if not _is_int(product_id):
            raise DataError(f"item {index}: product_id must be an int")
        qty = _require_int(f"item {index}: qty", item.get("qty"), minimum=1)
        discount = _require_int(f"item {index}: discount_idr", item.get("discount_idr", 0))
        lines.append((product_id, qty, discount))
    return lines


def record_sale(
    conn: sqlite3.Connection,
    business_id: int,
    items: Sequence[dict],
    clock: Clock,
    discount_idr_total: int = 0,
) -> int:
    """Record a sale and reduce stock in one transaction; return the sale id.

    ``items`` is a list of ``{product_id, qty, discount_idr=0}``. The unit price
    is read from the product at sale time. ``discount_idr_total`` is an extra
    sale-wide discount, allocated to the lines in order (each line up to its
    remaining line total) and stored in ``sale_item.discount_idr``, so
    ``sale.total_idr`` always equals the sum of ``qty * unit_price_idr -
    discount_idr`` over the stored items.

    The whole sale is rejected (nothing written, stock unchanged) when an item
    is malformed, a product is missing or belongs to another business, stock is
    short (duplicate lines of one product are merged for this check), or a
    discount is larger than what it applies to.
    """
    lines = _validate_items(items)
    _require_int("discount_idr_total", discount_idr_total)

    with transaction(conn):
        products: dict[int, sqlite3.Row] = {}
        for product_id, _, _ in lines:
            if product_id in products:
                continue
            row = conn.execute(
                "SELECT id, name, qty, unit_price_idr FROM product WHERE id = ? AND business_id = ?",
                (product_id, business_id),
            ).fetchone()
            if row is None:
                raise UnknownProductError(f"product {product_id} not found in this business")
            products[product_id] = row

        wanted: dict[int, int] = {}
        for product_id, qty, _ in lines:
            wanted[product_id] = wanted.get(product_id, 0) + qty
        for product_id, total_qty in wanted.items():
            row = products[product_id]
            if total_qty > row["qty"]:
                raise StockError(
                    f"not enough stock for {row['name']!r} (id {product_id}): "
                    f"need {total_qty}, have {row['qty']}"
                )

        remaining_discount = discount_idr_total
        priced: list[tuple[int, int, int, int]] = []  # product_id, qty, unit_price, discount
        total = 0
        for product_id, qty, discount in lines:
            unit_price = products[product_id]["unit_price_idr"]
            line_total = qty * unit_price
            if discount > line_total:
                raise DataError(
                    f"discount {discount} is above the line total {line_total} "
                    f"for product {product_id}"
                )
            extra = min(remaining_discount, line_total - discount)
            remaining_discount -= extra
            discount += extra
            priced.append((product_id, qty, unit_price, discount))
            total += line_total - discount
        if remaining_discount:
            raise DataError("discount_idr_total is above the total of the sale")

        cur = conn.execute(
            "INSERT INTO sale (business_id, created_at, total_idr) VALUES (?, ?, ?)",
            (business_id, _iso(clock.now()), total),
        )
        sale_id = int(cur.lastrowid)
        for product_id, qty, unit_price, discount in priced:
            conn.execute(
                "INSERT INTO sale_item (sale_id, product_id, qty, unit_price_idr, discount_idr) "
                "VALUES (?, ?, ?, ?, ?)",
                (sale_id, product_id, qty, unit_price, discount),
            )
        for product_id, total_qty in wanted.items():
            cur = conn.execute(
                "UPDATE product SET qty = qty - ? WHERE id = ? AND business_id = ? AND qty >= ?",
                (total_qty, product_id, business_id, total_qty),
            )
            if cur.rowcount != 1:  # defensive: stock was checked above in this transaction
                raise StockError(f"stock update failed for product {product_id}")
        return sale_id


# ---------------------------------------------------------- purchase orders --


def place_purchase_order(
    conn: sqlite3.Connection,
    business_id: int,
    product_id: int,
    qty: int,
    cost_idr: int,
    status: str,
) -> int:
    """Insert a purchase order (``placed`` or ``pending``) and return its id.

    Stock is not changed here; it grows in :func:`receive_purchase`.
    """
    if status not in PURCHASE_OPEN_STATUSES:
        raise DataError("status must be 'placed' or 'pending'")
    _require_int("qty", qty, minimum=1)
    _require_int("cost_idr", cost_idr)
    if not _is_int(product_id):
        raise DataError("product_id must be an int")
    with transaction(conn):
        if get_product(conn, business_id, product_id) is None:
            raise UnknownProductError(f"product {product_id} not found in this business")
        cur = conn.execute(
            "INSERT INTO purchase_order (product_id, qty, cost_idr, status) VALUES (?, ?, ?, ?)",
            (product_id, qty, cost_idr, status),
        )
        return int(cur.lastrowid)


def receive_purchase(
    conn: sqlite3.Connection,
    business_id: int,
    purchase_order_id: int,
    clock: Clock | None = None,
) -> int:
    """Receive a ``placed`` order: add its qty to stock and mark it ``received``.

    Both changes happen in one transaction. A ``pending`` order, an unknown
    order, an order of another business, or an order that was already received
    raises :class:`PurchaseError` and changes nothing. Returns the product's new
    stock quantity.
    """
    clock = clock or SystemClock()
    if not _is_int(purchase_order_id):
        raise DataError("purchase_order_id must be an int")
    with transaction(conn):
        row = conn.execute(
            "SELECT po.id, po.product_id, po.qty, po.status FROM purchase_order po "
            "JOIN product p ON p.id = po.product_id "
            "WHERE po.id = ? AND p.business_id = ?",
            (purchase_order_id, business_id),
        ).fetchone()
        if row is None:
            raise PurchaseError(f"purchase order {purchase_order_id} not found in this business")
        if row["status"] != "placed":
            raise PurchaseError(
                f"purchase order {purchase_order_id} is {row['status']!r}, only 'placed' can be received"
            )
        cur = conn.execute(
            "UPDATE purchase_order SET status = 'received', received_at = ? "
            "WHERE id = ? AND status = 'placed'",
            (_iso(clock.now()), purchase_order_id),
        )
        if cur.rowcount != 1:  # defensive
            raise PurchaseError(f"purchase order {purchase_order_id} could not be received")
        conn.execute("UPDATE product SET qty = qty + ? WHERE id = ?", (row["qty"], row["product_id"]))
        return int(conn.execute("SELECT qty FROM product WHERE id = ?", (row["product_id"],)).fetchone()[0])


# ------------------------------------------------------------- sales totals --


def _as_local_day(day: str | date) -> date:
    if isinstance(day, datetime):
        raise DataError("day must be a date or a YYYY-MM-DD string")
    if isinstance(day, date):
        return day
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        raise DataError("day must be a date or a YYYY-MM-DD string") from None


def _utc_range(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tzinfo=JAKARTA)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=JAKARTA)
    return _iso(start), _iso(end)


def sales_total_for_day(conn: sqlite3.Connection, business_id: int, day: str | date) -> int:
    """Sum of ``sale.total_idr`` for one Asia/Jakarta calendar day (0 if none)."""
    start, end = _utc_range(_as_local_day(day))
    row = conn.execute(
        "SELECT COALESCE(SUM(total_idr), 0) FROM sale "
        "WHERE business_id = ? AND created_at >= ? AND created_at < ?",
        (business_id, start, end),
    ).fetchone()
    return int(row[0])


def sales_by_day(
    conn: sqlite3.Connection,
    business_id: int,
    days: int | Iterable[str | date],
    clock: Clock | None = None,
) -> dict[str, int]:
    """Sales total per Jakarta day, oldest first, as ``{YYYY-MM-DD: total_idr}``.

    ``days`` is either an iterable of days, or an int N meaning the last N days
    ending today (today comes from ``clock``, default the system clock). Days
    without sales appear with 0.
    """
    if isinstance(days, (bool, float)):
        raise DataError("days must be a positive int or an iterable of days")
    if _is_int(days):
        _require_int("days", days, minimum=1)
        today = _as_local_day((clock or SystemClock()).today())
        wanted = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    else:
        wanted = [_as_local_day(d) for d in days]
    return {d.isoformat(): sales_total_for_day(conn, business_id, d) for d in wanted}
