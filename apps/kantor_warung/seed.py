"""Demo data for Kantor Warung (Req 10.1): products, stock and a week of sales.

A fresh Warung starts with 12 everyday products of an Indonesian warung, at
plausible rupiah prices and costs, and 7 days of sales history, so the demo runs
without any manual data entry. Everything is written through the data layer
(``add_product`` and ``record_sale``). There is no raw SQL on the money tables.

What the data is built to show
------------------------------
* Three products start below their minimum stock (gula, susu kental manis, kecap).
  With the default daily limit of Rp 100.000 the kecap order fits and the other
  two do not, so the stock agent shows both paths: a purchase and a pending order.
* The daily sales of the last three COMPLETE days decline strictly
  (Rp 1.120.000, then 940.000, then 780.000). That is the marketing agent's trigger
  (sales fell three days in a row), so it has something to react to on its first tick.

How the history is written
--------------------------
The history is planned first (which basket, at what time of which day), then the
products are added with ``final stock + units sold in the history``, then each sale
is recorded with a settable past clock so its ``created_at`` lies in its own Jakarta
day. ``record_sale`` reduces the stock, so after the history every product holds
exactly its ``final_qty``, and no sale can ever be short of stock.

The plan is deterministic (a fixed random seed), so every Warung starts the same.
Only ``seed_warung`` is used by the app. ``plan_history`` and the constants are
public for the tests.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from apps.kantor_warung import data
from skeleton.core.clock import JAKARTA, Clock, FakeClock

SEED_KEY = "kantor-warung-demo-v1"
OPEN_MINUTE = 7 * 60  # the first sale of a day is at 07:00 Jakarta time or later
CLOSE_MINUTE = 21 * 60  # and the last one before 21:00
SLACK_IDR = 25_000  # a day's total lands within this much below its target
MAX_BASKET_TRIES = 600


@dataclass(frozen=True)
class CatalogItem:
    name: str
    price_idr: int
    cost_idr: int
    min_qty: int
    final_qty: int  # stock the owner sees after the history is written
    weight: int  # how often it appears in a basket
    max_per_basket: int = 3


CATALOG: tuple[CatalogItem, ...] = (
    CatalogItem("Beras Premium 5 kg", 72_000, 66_000, min_qty=6, final_qty=14, weight=4, max_per_basket=2),
    CatalogItem("Minyak Goreng 1 L", 18_000, 16_000, min_qty=8, final_qty=20, weight=5),
    CatalogItem("Gula Pasir 1 kg", 17_500, 15_500, min_qty=6, final_qty=3, weight=5),  # below minimum
    CatalogItem("Telur Ayam 1 kg", 30_000, 27_500, min_qty=5, final_qty=11, weight=6, max_per_basket=2),
    CatalogItem("Mie Instan Goreng", 3_500, 2_900, min_qty=30, final_qty=85, weight=10, max_per_basket=5),
    CatalogItem("Kopi Sachet (10 pcs)", 14_000, 11_500, min_qty=10, final_qty=24, weight=7),
    CatalogItem("Teh Celup 25 pcs", 8_500, 6_800, min_qty=8, final_qty=17, weight=4),
    CatalogItem("Susu Kental Manis", 12_500, 10_800, min_qty=8, final_qty=4, weight=4),  # below minimum
    CatalogItem("Sabun Mandi", 4_500, 3_600, min_qty=12, final_qty=30, weight=3),
    CatalogItem("Deterjen Bubuk 800 g", 21_000, 18_000, min_qty=6, final_qty=9, weight=2, max_per_basket=2),
    CatalogItem("Air Mineral 600 ml", 3_500, 2_400, min_qty=24, final_qty=48, weight=8, max_per_basket=4),
    CatalogItem("Kecap Manis 275 ml", 11_000, 9_200, min_qty=4, final_qty=2, weight=3),  # below minimum
)

# Target sales per day, oldest first (7 complete days before today). The last three
# decline strictly, with gaps far larger than SLACK_IDR.
DAY_TARGETS_IDR: tuple[int, ...] = (
    1_050_000,
    1_150_000,
    1_300_000,
    1_200_000,
    1_120_000,
    940_000,
    780_000,
)
HISTORY_DAYS = len(DAY_TARGETS_IDR)


@dataclass(frozen=True)
class PlannedSale:
    at: datetime  # Jakarta time
    lines: tuple[tuple[str, int], ...]  # (product name, qty)


def _basket(rng: random.Random) -> tuple[tuple[str, int], ...]:
    count = rng.choices((1, 2, 3, 4), weights=(35, 35, 20, 10))[0]
    picked: dict[str, int] = {}
    pool = list(CATALOG)
    for _ in range(count):
        if not pool:
            break
        item = rng.choices(pool, weights=[p.weight for p in pool])[0]
        pool.remove(item)
        picked[item.name] = rng.randint(1, item.max_per_basket)
    return tuple(picked.items())


def _value(lines: tuple[tuple[str, int], ...]) -> int:
    price = {item.name: item.price_idr for item in CATALOG}
    return sum(price[name] * qty for name, qty in lines)


def plan_history(today: date) -> list[PlannedSale]:
    """The week of sales before ``today``, oldest first. Deterministic."""
    rng = random.Random(SEED_KEY)
    planned: list[PlannedSale] = []
    for index, target in enumerate(DAY_TARGETS_IDR):
        day = today - timedelta(days=HISTORY_DAYS - index)
        baskets: list[tuple[tuple[str, int], ...]] = []
        total = 0
        for _ in range(MAX_BASKET_TRIES):
            if target - total <= SLACK_IDR:
                break
            basket = _basket(rng)
            value = _value(basket)
            if total + value <= target:  # never overshoot, so days stay in the planned order
                baskets.append(basket)
                total += value
        minutes = sorted(rng.sample(range(OPEN_MINUTE, CLOSE_MINUTE), len(baskets)))
        for minute, basket in zip(minutes, baskets):
            at = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=JAKARTA)
            planned.append(PlannedSale(at=at, lines=basket))
    planned.sort(key=lambda sale: sale.at)
    return planned


def seed_warung(conn: sqlite3.Connection, business_id: int, clock: Clock) -> None:
    """Add the products and the sales history to a new Warung. Does nothing if it has products."""
    if data.list_products(conn, business_id):
        return
    history = plan_history(date.fromisoformat(clock.today()))
    sold: dict[str, int] = {item.name: 0 for item in CATALOG}
    for sale in history:
        for name, qty in sale.lines:
            sold[name] += qty

    product_ids: dict[str, int] = {}
    for item in CATALOG:
        product_ids[item.name] = data.add_product(
            conn,
            business_id,
            item.name,
            qty=item.final_qty + sold[item.name],  # the history takes sold[...] units out again
            min_qty=item.min_qty,
            unit_price_idr=item.price_idr,
            unit_cost_idr=item.cost_idr,
        )

    past = FakeClock()
    for sale in history:
        past.set(sale.at)  # created_at lies in the sale's own Jakarta day
        data.record_sale(
            conn,
            business_id,
            [{"product_id": product_ids[name], "qty": qty} for name, qty in sale.lines],
            past,
        )
