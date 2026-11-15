"""Kantor Warung roles: cashier, stock, finance and marketing (Req 1.2, 3.2, 3.3, 5.4, 6.3).

Every role is a :class:`BaseRole`. ``triggers`` is pure rule logic: it reads the
database and the clock, makes no model call and writes nothing (Req 3.3). Model
text is added later by the Manager, only for ``draft`` and ``customer_message``
tasks. All money is integer rupiah.

Who does what
-------------
* **Cashier** records sales from a pluggable :class:`SalesFeed`. A recorded sale
  moves no money out and sends nothing, so its tasks are kind ``read``.
* **Stock** reorders products below their minimum (Req 6.3). When the order fits
  the remaining daily limit it emits a ``spend`` task handed to the finance
  agent (so the spend is booked to finance). When it does not fit, it records a
  ``pending`` purchase order for the owner. Placed orders are received after a
  simulated lead time.
* **Finance** writes one daily summary after a configurable hour and executes
  the purchase spends handed over by the stock agent.
* **Marketing** reacts to three strictly declining sales days with one promo
  idea (``draft``) and one promo broadcast (``customer_message``, outbox only).

Idempotency without module state
--------------------------------
The ``purchase_order`` table has no creation time, so when stock places an order
it also writes one ``purchase_order`` activity row ("PO#<id> dipesan"). Its
timestamp is the order time used for the delivery lead time. An order without
such a row (for example a manual one) counts as already old enough. "Done today"
checks (summary, promo) read the activity log the same way. Because of this the
activity log rows are part of the roles' behavior and must not be removed.

Labels and bubbles
------------------
Each task carries a friendly Indonesian ``label`` (what the activity feed shows as the action,
for example "Catat penjualan") and a short ``bubble`` text (30 to 45 characters, shown above the
agent's head while it works). The policy engine still reads ``kind``. Because the activity log
stores the label as ``action``, the "done today" checks look for the label and also for the old
kind word, so a database written by an older version keeps working.

Demo hours and volume
---------------------
``SimulatedSalesFeed(demo_hours=True)`` ignores the shop hours, so the demo is lively at night too
(``--real-hours`` turns the hours back on). With a short heartbeat interval (``interval_seconds``)
the feed sells ``DEMO_BOOST`` times faster than real time instead of ``300 / interval`` times, so a
short interval does not drain the stock faster than the stock agent can restock. In demo mode the
stock role also receives a delivery after ``DEMO_LEAD_TIME`` (1 minute) instead of 2 hours.

Depends only on ``skeleton.*``; the skeleton never imports this module.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from apps.kantor_warung import data
from apps.kantor_warung.data import DataError, PurchaseError, StockError, UnknownProductError
from skeleton.core.clock import JAKARTA, Clock
from skeleton.log.activity import record
from skeleton.policy import remaining_today, spent_today
from skeleton.runtime.demo_mode import demo_speed
from skeleton.runtime.models import ActionResult, BaseRole, BusinessContext, Role, Task, TaskKind
from skeleton.runtime.registry import RoleRegistry

logger = logging.getLogger(__name__)

ROLE_CASHIER = "cashier"
ROLE_STOCK = "stock"
ROLE_FINANCE = "finance"
ROLE_MARKETING = "marketing"

DEFAULT_LEAD_TIME = timedelta(hours=2)
DEMO_LEAD_TIME = timedelta(minutes=1)  # demo mode: 2 hours of simulated time is about 1 minute of real time
DEFAULT_FINANCE_HOUR = 17
REORDER_TARGET_FACTOR = 3  # reorder enough to reach 3x min_qty
HANDOFF_KEY = "handoff_to"
PROMO_CHANNEL = "whatsapp"
PROMO_RECIPIENT = "pelanggan setia"
BUBBLE_MAX_CHARS = 60
BUBBLE_TEXT_CHARS = 45  # short bubble texts of the roles

# Activity rows used as "done" markers (see the module docstring).
ACTION_PURCHASE_ORDER = "Catat pesanan"  # written by the stock role itself (older versions wrote "purchase_order")
_OLD_PURCHASE_ORDER_ACTION = "purchase_order"
SUMMARY_REASON = "Ringkasan harian"
PROMO_IDEA_REASON = "Ide promo"
PROMO_MESSAGE_REASON = "Promo pelanggan setia"

# Friendly action words for the activity feed (Task.label).
LABEL_SALE = "Catat penjualan"
LABEL_REORDER = "Pesan stok"
LABEL_REORDER_PENDING = "Ajukan pesanan stok"
LABEL_RECEIVE = "Terima kiriman"
LABEL_SUMMARY = "Ringkasan harian"
LABEL_PROMO_IDEA = "Ide promo"
LABEL_PROMO_MESSAGE = "Kirim promo"

# Payload "action" values, used to route a task to its handler in ``run``.
ACT_SALE = "sale"
ACT_REORDER = "reorder"
ACT_REORDER_PENDING = "reorder_pending"
ACT_RECEIVE = "receive"
ACT_SUMMARY = "daily_summary"
ACT_PROMO_IDEA = "promo_idea"
ACT_PROMO_BROADCAST = "promo_broadcast"


# ------------------------------------------------------------------ helpers --


def rupiah(amount: int) -> str:
    """Format integer rupiah the Indonesian way: ``Rp 54.000``."""
    return "Rp " + f"{amount:,}".replace(",", ".")


def _short(text: str, limit: int = BUBBLE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _bubble(text: str) -> str:
    """A short speech-bubble text (at most ``BUBBLE_TEXT_CHARS`` characters)."""
    return _short(text, BUBBLE_TEXT_CHARS)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(moment: datetime) -> str:
    # Same fixed format as skeleton/log/activity.py, so string order is time order.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _day_bounds(day: str) -> tuple[str, str]:
    """UTC ISO range ``[start, end)`` of one Asia/Jakarta calendar day."""
    local_day = date.fromisoformat(day)
    start = datetime.combine(local_day, time.min, tzinfo=JAKARTA)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=JAKARTA)
    return _iso(start), _iso(end)


def _find_agent(ctx: BusinessContext, role_name: str, *, active_only: bool = False) -> int | None:
    sql = "SELECT id FROM agent WHERE business_id = ? AND role = ?"
    if active_only:
        sql += " AND paused = 0"
    row = ctx.conn.execute(sql + " ORDER BY id LIMIT 1", (ctx.business_id, role_name)).fetchone()
    return int(row[0]) if row else None


def _logged_today(
    ctx: BusinessContext, agent_id: int, actions: tuple[str, ...], reason_prefix: str
) -> bool:
    """True when this agent already has a non-failed activity row today (Jakarta).

    ``actions`` are the words a row may carry: the task's label and, for rows written by an older
    version, its kind.
    """
    start, end = _day_bounds(ctx.clock.today())
    marks = ",".join("?" for _ in actions)
    row = ctx.conn.execute(
        f"SELECT 1 FROM activity WHERE business_id = ? AND agent_id = ? AND action IN ({marks}) "
        "AND reason LIKE ? AND status != 'failed' AND created_at >= ? AND created_at < ? LIMIT 1",
        (ctx.business_id, agent_id, *actions, reason_prefix + "%", start, end),
    ).fetchone()
    return row is not None


def _open_orders(ctx: BusinessContext, statuses: tuple[str, ...] = ("placed", "pending")) -> list[dict]:
    marks = ",".join("?" for _ in statuses)
    rows = ctx.conn.execute(
        "SELECT po.id, po.product_id, po.qty, po.cost_idr, po.status, p.name "
        "FROM purchase_order po JOIN product p ON p.id = po.product_id "
        f"WHERE p.business_id = ? AND po.status IN ({marks}) ORDER BY po.id",
        (ctx.business_id, *statuses),
    ).fetchall()
    return [
        {"id": r[0], "product_id": r[1], "qty": r[2], "cost_idr": r[3], "status": r[4], "name": r[5]}
        for r in rows
    ]


def _has_open_order(ctx: BusinessContext, product_id: int) -> bool:
    return any(o["product_id"] == product_id for o in _open_orders(ctx))


def _order_placed_at(ctx: BusinessContext, purchase_order_id: int) -> datetime | None:
    row = ctx.conn.execute(
        "SELECT created_at FROM activity WHERE business_id = ? AND action IN (?, ?) AND reason LIKE ? "
        "ORDER BY id LIMIT 1",
        (ctx.business_id, ACTION_PURCHASE_ORDER, _OLD_PURCHASE_ORDER_ACTION, f"PO#{purchase_order_id} dipesan%"),
    ).fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def _item_summary(items: list[dict], names: dict[int, str]) -> str:
    return ", ".join(f"{i['qty']}x {names.get(i['product_id'], 'produk #' + str(i['product_id']))}" for i in items)


class _WarungRole(BaseRole):
    """Shared plumbing: which agent row this role instance belongs to."""

    def __init__(self, agent_id: int | None = None) -> None:
        # None means "look the agent up by role name" (used by RoleRegistry.create).
        self.agent_id = agent_id

    def _me(self, ctx: BusinessContext) -> int | None:
        return self.agent_id if self.agent_id is not None else _find_agent(ctx, self.name)

    def run(self, task: Task, ctx: BusinessContext) -> ActionResult:
        handler = self._handlers().get(task.payload.get("action"))
        if handler is None:
            return ActionResult(status="info", message=f"{self.name}: tidak ada yang dikerjakan")
        return handler(task, ctx)

    def _handlers(self) -> dict[str, Any]:
        return {}


# ----------------------------------------------------------------- the feed --


@runtime_checkable
class SalesFeed(Protocol):
    """Source of sales for the cashier (a POS, a simulator, ...)."""

    def poll(self, ctx: BusinessContext) -> list[dict]:
        """Return new sales as ``{"items": [{"product_id", "qty"}, ...]}`` dicts.

        Optional key ``discount_idr_total`` is passed to ``record_sale``. Must not
        write to the database.
        """
        ...


class SimulatedSalesFeed:
    """Deterministic pseudo walk-in customers for the demo.

    Each poll returns 0 to 3 sales of small baskets (1 to 3 products, 1 to 3 of
    each) built only from products in stock. The random generator is seeded from
    ``seed``, the Jakarta day, the hour and the 5-minute slot, so the same clock
    gives the same customers (reproducible), while different ticks differ (alive).
    The shop is closed before ``OPEN_HOUR`` and from ``CLOSE_HOUR``, unless ``demo_hours`` is set:
    then the feed ignores the hours, so the demo is lively at any hour. Volume is
    lower on some weekdays (Tuesday to Thursday), so three declining days happen
    and the marketing trigger can fire. Polling reads the database and writes nothing.

    ``interval_seconds`` is the heartbeat interval. At 300 s or more the feed sells at its normal
    pace (one poll per 5-minute slot). A shorter interval polls the same slot many times, so the
    chance of a sale per poll is scaled down (``demo_mode.demo_speed``, ``DEMO_BOOST`` times
    faster than real time): about one sale per minute at most, whatever the interval.
    """

    OPEN_HOUR = 7
    CLOSE_HOUR = 21
    SLOT_MINUTES = 5
    MAX_SALES = 3
    BASE_CHANCE = 0.3  # chance per possible sale slot at weight 1.0
    PEAK_HOURS = frozenset({11, 12, 17, 18})
    # Monday=0 ... Sunday=6. Monday to Thursday decline, so those days can trigger marketing.
    DAY_WEIGHT = (1.0, 0.8, 0.6, 0.4, 0.9, 1.1, 1.0)
    DEMO_BOOST = 6.0  # with a short interval the shop runs this many times faster than real time

    def __init__(
        self, seed: int = 0, *, demo_hours: bool = False, interval_seconds: float | None = None
    ) -> None:
        self.seed = seed
        self.demo_hours = demo_hours
        self.interval_seconds = interval_seconds
        # Which 5-minute slot was polled last, and how many times. With a short heartbeat
        # (``--interval 10``) one slot is polled many times; each poll must be a new set of
        # customers, not the same basket again. The first poll of a slot (the only one with
        # the default 300 s interval) keeps its original seed.
        self._slot: str | None = None
        self._polls_in_slot = 0

    def poll(self, ctx: BusinessContext) -> list[dict]:
        local = ctx.clock.now().astimezone(JAKARTA)
        if not self.demo_hours and not self.OPEN_HOUR <= local.hour < self.CLOSE_HOUR:
            return []
        slot = f"{self.seed}|{local.date().isoformat()}|{local.hour}|{local.minute // self.SLOT_MINUTES}"
        self._polls_in_slot = self._polls_in_slot + 1 if slot == self._slot else 0
        self._slot = slot
        rng = random.Random(slot if self._polls_in_slot == 0 else f"{slot}|{self._polls_in_slot}")
        weight = self.DAY_WEIGHT[local.weekday()] * (1.6 if local.hour in self.PEAK_HOURS else 1.0)
        chance = min(0.9, self.BASE_CHANCE * weight) * demo_speed(self.interval_seconds, self.DEMO_BOOST)
        count = sum(rng.random() < chance for _ in range(self.MAX_SALES))

        available = {p["id"]: p["qty"] for p in data.list_products(ctx.conn, ctx.business_id) if p["qty"] > 0}
        sales: list[dict] = []
        for _ in range(count):
            pool = [pid for pid, qty in available.items() if qty > 0]
            if not pool:
                break
            items = []
            for product_id in rng.sample(pool, rng.randint(1, min(3, len(pool)))):
                qty = rng.randint(1, min(3, available[product_id]))
                available[product_id] -= qty
                items.append({"product_id": product_id, "qty": qty})
            sales.append({"items": items})
        return sales


# ------------------------------------------------------------------ cashier --


class CashierRole(_WarungRole):
    """Records sales from a feed. No feed means no sales."""

    name = ROLE_CASHIER

    def __init__(self, agent_id: int | None = None, feed: SalesFeed | None = None) -> None:
        super().__init__(agent_id)
        self.feed = feed

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if self.feed is None or me is None:
            return []
        names = {p["id"]: p["name"] for p in data.list_products(ctx.conn, ctx.business_id)}
        tasks = []
        for sale in self.feed.poll(ctx):
            items = [
                {"product_id": i["product_id"], "qty": i["qty"]}
                for i in sale.get("items", [])
                if isinstance(i, dict) and "product_id" in i and "qty" in i
            ]
            if not items:
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={
                        "action": ACT_SALE,
                        "items": items,
                        "discount_idr_total": sale.get("discount_idr_total", 0),
                    },
                    cost_idr=0,
                    reason=f"Catat penjualan: {_item_summary(items, names)}",
                    label=LABEL_SALE,
                    bubble=_bubble(f"Mencatat {names.get(items[0]['product_id'], 'penjualan')}"
                                   + (f" +{len(items) - 1} lain" if len(items) > 1 else "")),
                )
            )
        return tasks

    def _handlers(self) -> dict[str, Any]:
        return {ACT_SALE: self._record_sale}

    def _record_sale(self, task: Task, ctx: BusinessContext) -> ActionResult:
        items = task.payload.get("items", [])
        try:
            sale_id = data.record_sale(
                ctx.conn,
                ctx.business_id,
                items,
                ctx.clock,
                task.payload.get("discount_idr_total", 0),
            )
        except StockError:
            return ActionResult(status="failed", message=_short(f"Gagal: stok {self._short_names(ctx, items)} kurang"))
        except UnknownProductError:
            return ActionResult(status="failed", message="Gagal: produk tidak dikenal")
        except DataError as exc:
            return ActionResult(status="failed", message=_short(f"Gagal catat penjualan: {exc}", 120))
        total = ctx.conn.execute("SELECT total_idr FROM sale WHERE id = ?", (sale_id,)).fetchone()[0]
        return ActionResult(
            status="done",
            message=f"Terjual {rupiah(total)} ({len(items)} produk)",
            data={"sale_id": sale_id, "total_idr": total},
        )

    @staticmethod
    def _short_names(ctx: BusinessContext, items: list[dict]) -> str:
        """Names of the products that are short, for the failure message."""
        wanted: dict[int, int] = {}
        for item in items:
            if isinstance(item, dict) and _is_int(item.get("product_id")) and _is_int(item.get("qty")):
                wanted[item["product_id"]] = wanted.get(item["product_id"], 0) + item["qty"]
        short = []
        for product_id, qty in wanted.items():
            product = data.get_product(ctx.conn, ctx.business_id, product_id)
            if product is not None and qty > product["qty"]:
                short.append(product["name"])
        return ", ".join(short) or "produk"


# -------------------------------------------------------------------- stock --


def _execute_reorder(task: Task, ctx: BusinessContext) -> ActionResult:
    """Place the purchase order of a reorder task. Used by stock and by finance.

    A ``spend`` task (already authorized by the Manager) places a ``placed``
    order for the authorized cost. A ``read`` task records a ``pending`` order.
    An open order for the product is never doubled.
    """
    payload = task.payload
    product_id, qty = payload.get("product_id"), payload.get("qty")
    if not _is_int(product_id) or not _is_int(qty) or qty < 1:
        return ActionResult(status="failed", message="Gagal: data pesanan tidak valid")
    product = data.get_product(ctx.conn, ctx.business_id, product_id)
    if product is None:
        return ActionResult(status="failed", message="Gagal: produk tidak dikenal")
    name = product["name"]
    if _has_open_order(ctx, product_id):
        return ActionResult(status="info", message=_short(f"Sudah ada pesanan {name}"))

    if task.kind == TaskKind.SPEND:
        cost = task.cost_idr
        po_id = data.place_purchase_order(ctx.conn, ctx.business_id, product_id, qty, cost, "placed")
        try:
            record(
                ctx.conn, ctx.business_id, task.agent_id, ACTION_PURCHASE_ORDER,
                f"PO#{po_id} dipesan: {qty}x {name}", 0, "info", ctx.clock, ctx.bus,
            )
        except Exception:  # without the row the order is simply treated as old enough
            logger.exception("could not write the purchase order marker")
        return ActionResult(
            status="done",
            message=_short(f"Pesan {qty}x {name} ({rupiah(cost)})"),
            cost_idr=cost,
            data={"purchase_order_id": po_id},
        )

    cost = payload.get("cost_idr", qty * product["unit_cost_idr"])
    po_id = data.place_purchase_order(ctx.conn, ctx.business_id, product_id, qty, cost, "pending")
    return ActionResult(
        status="pending",
        message=_short(f"Menunggu pemilik: {name} {rupiah(cost)}, limit harian"),
        data={"purchase_order_id": po_id},
        bubble=_bubble(f"Menunggu OK: {name}"),
    )


class StockRole(_WarungRole):
    """Reorders low stock within the daily limit and receives deliveries (Req 6.3)."""

    name = ROLE_STOCK

    def __init__(self, agent_id: int | None = None, lead_time: timedelta = DEFAULT_LEAD_TIME) -> None:
        # ``DEMO_LEAD_TIME`` in demo mode (see build_roles(demo=True)).
        super().__init__(agent_id)
        self.lead_time = lead_time

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None:
            return []
        return self._deliveries(ctx, me) + self._reorders(ctx, me)

    def _deliveries(self, ctx: BusinessContext, me: int) -> list[Task]:
        now = ctx.clock.now()
        tasks = []
        for order in _open_orders(ctx, ("placed",)):
            placed_at = _order_placed_at(ctx, order["id"])
            if placed_at is not None and now - placed_at < self.lead_time:
                continue
            tasks.append(
                Task(
                    agent_id=me,
                    kind=TaskKind.READ,
                    payload={
                        "action": ACT_RECEIVE,
                        "purchase_order_id": order["id"],
                        "product_id": order["product_id"],
                    },
                    cost_idr=0,
                    reason=f"Terima kiriman: {order['qty']}x {order['name']}",
                    label=LABEL_RECEIVE,
                    bubble=_bubble(f"Menerima kiriman {order['name']}"),
                )
            )
        return tasks

    def _reorders(self, ctx: BusinessContext, me: int) -> list[Task]:
        ordered = {o["product_id"] for o in _open_orders(ctx)}
        budget = remaining_today(ctx.conn, ctx.business_id, ctx.clock)
        finance = _find_agent(ctx, ROLE_FINANCE, active_only=True)
        tasks = []
        for product in data.low_stock(ctx.conn, ctx.business_id):
            if product["id"] in ordered:
                continue
            qty = max(1, REORDER_TARGET_FACTOR * product["min_qty"] - product["qty"])
            cost = qty * product["unit_cost_idr"]
            payload = {
                "action": ACT_REORDER,
                "subject": product["name"],  # named in a rejection row ("Pesan stok Teh Botol Rp ... ditolak")
                "product_id": product["id"],
                "product_name": product["name"],
                "qty": qty,
                "cost_idr": cost,
            }
            facts = f"{product['name']} tinggal {product['qty']} (min {product['min_qty']})"
            if cost <= budget:
                budget -= cost  # several orders in one tick must fit the limit together
                if finance is not None and finance != me:
                    payload[HANDOFF_KEY] = finance
                tasks.append(
                    Task(
                        agent_id=me,
                        kind=TaskKind.SPEND,
                        payload=payload,
                        cost_idr=cost,
                        reason=f"Stok menipis, {facts}: pesan {qty} pcs ({rupiah(cost)})",
                        label=LABEL_REORDER,
                        bubble=_bubble(f"Memesan {product['name']}"),
                    )
                )
            else:
                payload["action"] = ACT_REORDER_PENDING
                tasks.append(
                    Task(
                        agent_id=me,
                        kind=TaskKind.READ,
                        payload=payload,
                        cost_idr=0,
                        reason=(
                            f"Stok menipis, {facts}: pesanan {rupiah(cost)} melebihi sisa limit "
                            f"harian {rupiah(budget)}, menunggu pemilik"
                        ),
                        label=LABEL_REORDER_PENDING,
                        bubble=_bubble(f"Mengajukan pesanan {product['name']}"),
                    )
                )
        return tasks

    def _handlers(self) -> dict[str, Any]:
        return {
            ACT_REORDER: _execute_reorder,
            ACT_REORDER_PENDING: _execute_reorder,
            ACT_RECEIVE: self._receive,
        }

    @staticmethod
    def _receive(task: Task, ctx: BusinessContext) -> ActionResult:
        order_id = task.payload.get("purchase_order_id")
        try:
            new_qty = data.receive_purchase(ctx.conn, ctx.business_id, order_id, ctx.clock)
        except PurchaseError:
            return ActionResult(status="info", message="Pesanan sudah diterima")
        except DataError:
            return ActionResult(status="failed", message="Gagal: nomor pesanan tidak valid")
        product = data.get_product(ctx.conn, ctx.business_id, task.payload.get("product_id", -1))
        name = product["name"] if product else "produk"
        return ActionResult(
            status="done",
            message=_short(f"Kiriman {name} diterima, stok {new_qty}"),
            data={"purchase_order_id": order_id, "qty": new_qty},
        )


# ------------------------------------------------------------------ finance --


class FinanceRole(_WarungRole):
    """Daily summary after a set hour, plus the purchase spends handed over by stock."""

    name = ROLE_FINANCE

    def __init__(self, agent_id: int | None = None, summary_hour: int = DEFAULT_FINANCE_HOUR) -> None:
        super().__init__(agent_id)
        self.summary_hour = summary_hour

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None or ctx.clock.now().astimezone(JAKARTA).hour < self.summary_hour:
            return []
        if _logged_today(ctx, me, (LABEL_SUMMARY, TaskKind.DRAFT), SUMMARY_REASON):
            return []
        day = ctx.clock.today()
        start, end = _day_bounds(day)
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM sale WHERE business_id = ? AND created_at >= ? AND created_at < ?",
            (ctx.business_id, start, end),
        ).fetchone()[0]
        facts = {
            "action": ACT_SUMMARY,
            "day": day,
            "sales_total_idr": data.sales_total_for_day(ctx.conn, ctx.business_id, day),
            "sales_count": count,
            "spent_idr": spent_today(ctx.conn, ctx.business_id, ctx.clock),
            "waiting_orders": len(_open_orders(ctx, ("pending",))),
        }
        return [
            Task(
                agent_id=me,
                kind=TaskKind.DRAFT,
                payload=facts,
                cost_idr=0,
                reason=f"{SUMMARY_REASON} {day}",
                label=LABEL_SUMMARY,
                bubble="Menyusun ringkasan harian",
            )
        ]

    def _handlers(self) -> dict[str, Any]:
        # Purchase spends handed over by the stock agent are executed here.
        return {ACT_SUMMARY: self._summary, ACT_REORDER: _execute_reorder}

    def _summary(self, task: Task, ctx: BusinessContext) -> ActionResult:
        text = task.payload.get("llm_text")
        source = "llm"
        if not isinstance(text, str) or not text.strip():
            text, source = self._rule_summary(ctx), "rule"
        text = text.strip()
        return ActionResult(status="done", message=text, data={"summary": text, "source": source})

    @staticmethod
    def _rule_summary(ctx: BusinessContext) -> str:
        day = ctx.clock.today()
        start, end = _day_bounds(day)
        sales = data.sales_total_for_day(ctx.conn, ctx.business_id, day)
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM sale WHERE business_id = ? AND created_at >= ? AND created_at < ?",
            (ctx.business_id, start, end),
        ).fetchone()[0]
        spent = spent_today(ctx.conn, ctx.business_id, ctx.clock)
        left = remaining_today(ctx.conn, ctx.business_id, ctx.clock)
        parts = [
            f"Ringkasan {day}: penjualan {rupiah(sales)} dari {count} transaksi.",
            f"Belanja stok {rupiah(spent)}, sisa limit {rupiah(left)}.",
        ]
        waiting = _open_orders(ctx, ("pending",))
        if waiting:
            listed = ", ".join(f"{o['name']} ({rupiah(o['cost_idr'])})" for o in waiting)
            parts.append(f"Menunggu keputusan pemilik: {listed}.")
        else:
            parts.append("Tidak ada pesanan yang menunggu pemilik.")
        low = data.low_stock(ctx.conn, ctx.business_id)
        if low:
            parts.append("Stok menipis: " + ", ".join(p["name"] for p in low) + ".")
        return " ".join(parts)


# ---------------------------------------------------------------- marketing --


class MarketingRole(_WarungRole):
    """One promo idea and one promo broadcast when sales fall three days in a row.

    ``template_body=False`` (default) leaves ``payload["body"]`` empty, so the
    Manager drafts the broadcast with the strong model. ``template_body=True``
    puts a rule-based template in ``payload["body"]`` and the Manager keeps it.
    """

    name = ROLE_MARKETING
    DECLINE_DAYS = 3

    def __init__(self, agent_id: int | None = None, template_body: bool = False) -> None:
        super().__init__(agent_id)
        self.template_body = template_body

    def triggers(self, ctx: BusinessContext) -> list[Task]:
        me = self._me(ctx)
        if me is None:
            return []
        today = date.fromisoformat(ctx.clock.today())
        days = [today - timedelta(days=n) for n in range(self.DECLINE_DAYS, 0, -1)]  # oldest first
        totals = data.sales_by_day(ctx.conn, ctx.business_id, days, ctx.clock)
        values = list(totals.values())
        if not all(later < earlier for earlier, later in zip(values, values[1:])):
            return []
        # Any customer_message row today counts, also a rejected one (its reason is the
        # policy's, not ours). Marketing sends nothing but promos, so this is one promo a day.
        if _logged_today(ctx, me, (LABEL_PROMO_MESSAGE, TaskKind.CUSTOMER_MESSAGE), ""):
            return []

        facts = {"sales_by_day": totals}
        message_payload: dict[str, Any] = {
            "action": ACT_PROMO_BROADCAST,
            "channel": PROMO_CHANNEL,
            "recipient": PROMO_RECIPIENT,
            **facts,
        }
        if self.template_body:
            message_payload["body"] = self._template(ctx)
        return [
            Task(
                agent_id=me,
                kind=TaskKind.DRAFT,
                payload={"action": ACT_PROMO_IDEA, **facts},
                cost_idr=0,
                reason=f"{PROMO_IDEA_REASON}: penjualan turun {self.DECLINE_DAYS} hari berturut-turut",
                label=LABEL_PROMO_IDEA,
                bubble="Memikirkan ide promo",
            ),
            Task(
                agent_id=me,
                kind=TaskKind.CUSTOMER_MESSAGE,
                payload=message_payload,
                cost_idr=0,
                reason=f"{PROMO_MESSAGE_REASON}: penjualan turun {self.DECLINE_DAYS} hari",
                label=LABEL_PROMO_MESSAGE,
                bubble="Menyiapkan pesan promo",
            ),
        ]

    def _handlers(self) -> dict[str, Any]:
        return {ACT_PROMO_IDEA: self._idea, ACT_PROMO_BROADCAST: self._broadcast}

    def _idea(self, task: Task, ctx: BusinessContext) -> ActionResult:
        text = task.payload.get("llm_text")
        source = "llm"
        if not isinstance(text, str) or not text.strip():
            text, source = f"Ide promo: beli 2 {self._featured(ctx)} diskon 10% minggu ini.", "rule"
        text = text.strip()
        return ActionResult(status="done", message=text, data={"idea": text, "source": source})

    @staticmethod
    def _broadcast(task: Task, ctx: BusinessContext) -> ActionResult:
        # The Manager wrote the outbox row before this runs; nothing is sent (Req 5.4).
        return ActionResult(status="done", message="Promo masuk outbox, menunggu pemilik")

    @staticmethod
    def _featured(ctx: BusinessContext) -> str:
        """Product with the most stock (ties: lowest id), the best one to push."""
        products = data.list_products(ctx.conn, ctx.business_id)
        if not products:
            return "produk andalan"
        return max(products, key=lambda p: (p["qty"], -p["id"]))["name"]

    def _template(self, ctx: BusinessContext) -> str:
        row = ctx.conn.execute("SELECT name FROM business WHERE id = ?", (ctx.business_id,)).fetchone()
        shop = row[0] if row else "kami"
        return (
            f"Halo pelanggan setia {shop}! Promo spesial: beli 2 {self._featured(ctx)} "
            "diskon 10%. Yuk mampir, kami tunggu!"
        )


# ------------------------------------------------------------ registry/build --

WARUNG_ROLES = RoleRegistry()
for _role_cls in (CashierRole, StockRole, FinanceRole, MarketingRole):
    WARUNG_ROLES.register(_role_cls)


def build_roles(
    conn: sqlite3.Connection,
    business_id: int,
    clock: Clock,
    *,
    feed: SalesFeed | None = None,
    finance_hour: int = DEFAULT_FINANCE_HOUR,
    demo: bool = False,
) -> dict[int, Role]:
    """Map each agent row of the business to a role instance, by its ``role`` column.

    Agents whose role is not a Warung role are skipped. ``feed`` goes to the
    cashier (``None`` means no sales source, so the cashier records nothing).
    ``demo=True`` gives the stock role the short demo delivery time (``DEMO_LEAD_TIME``).
    ``clock`` is accepted for a uniform signature; roles read the clock from the
    context they are called with.
    """
    roles: dict[int, Role] = {}
    rows = conn.execute(
        "SELECT id, role FROM agent WHERE business_id = ? ORDER BY id", (business_id,)
    ).fetchall()
    for agent_id, role_name in ((int(r[0]), r[1]) for r in rows):
        if role_name not in WARUNG_ROLES:
            continue
        role = WARUNG_ROLES.create(role_name)
        role.agent_id = agent_id
        if isinstance(role, CashierRole):
            role.feed = feed
        elif isinstance(role, StockRole):
            if demo:
                role.lead_time = DEMO_LEAD_TIME
        elif isinstance(role, FinanceRole):
            role.summary_hour = finance_hour
        roles[agent_id] = role
    return roles
