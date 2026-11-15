"""What the owner's decision does to a Warung purchase order (task 14.2).

The stock agent files a request of kind ``purchase_order`` when an order does not fit today's budget (see
``roles.py``). The pending ``purchase_order`` row stays the domain record. This module is the handler the app
registers in the skeleton's :class:`ApprovalHandlers`.

* **Approve:** the spend goes through the policy engine first (``authorize_spend``, kind ``spend``). The owner's
  approval does not override the daily limit: when the cost does not fit what is left today, the decision is
  refused with "Melebihi batas harian Rp X; naikkan batas lewat Aturan lalu setujui lagi", nothing is written and
  the request stays pending. When it fits, one ledger row is written, the order becomes ``placed`` and the usual
  "Catat pesanan" marker row is written, so the delivery arrives after the normal lead time. The stock agent
  receives it later (``receive_purchase``).
* **Reject:** the order becomes ``cancelled``. No money moves. The stock agent does not ask about the same
  product again for 24 hours (``rejected_recently``).

Depends only on ``skeleton.*`` and the other Warung modules; the skeleton never imports this module.
"""
from __future__ import annotations

import logging
import sqlite3

from apps.kantor_warung import data
from apps.kantor_warung.roles import ACTION_PURCHASE_ORDER, KIND_PURCHASE_ORDER, rupiah
from skeleton.core.clock import Clock
from skeleton.log.activity import record
from skeleton.policy.approvals import (
    ApprovalFailed,
    ApprovalHandlers,
    HandlerOutcome,
    authorize_spend,
)

logger = logging.getLogger(__name__)


def _order_of(conn: sqlite3.Connection, approval: dict) -> dict | None:
    po_id = approval["detail"].get("purchase_order_id")
    return data.get_purchase_order(conn, approval["business_id"], po_id)


class PurchaseOrderApproval:
    """Approve or reject a pending purchase order. Runs inside the decision's transaction."""

    def approve(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome:
        order = _order_of(conn, approval)
        if order is None or order["status"] != "pending":
            raise ApprovalFailed("Pesanan ini sudah tidak menunggu keputusan")
        cost = int(order["cost_idr"])
        # The order's own cost is the source of truth. This is the policy gate: it may raise ApprovalBlocked.
        authorize_spend(conn, approval["business_id"], approval["agent_id"], cost, clock)
        try:
            data.place_pending_order(conn, approval["business_id"], order["id"])
        except data.PurchaseError as exc:  # defensive: checked just above, in the same transaction
            raise ApprovalFailed("Pesanan ini sudah tidak menunggu keputusan") from exc
        name, qty = order["product_name"], order["qty"]
        # The marker row is the order time the delivery lead time counts from (see roles.py).
        record(
            conn, approval["business_id"], approval["agent_id"], ACTION_PURCHASE_ORDER,
            f"PO#{order['id']} dipesan: {qty}x {name}", 0, "info", clock,
        )
        return HandlerOutcome(message=f"Pesan {qty}x {name} ({rupiah(cost)}) dipesan", cost_idr=cost)

    def reject(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome:
        order = _order_of(conn, approval)
        if order is None or order["status"] != "pending":
            raise ApprovalFailed("Pesanan ini sudah tidak menunggu keputusan")
        data.cancel_pending_order(conn, approval["business_id"], order["id"])
        return HandlerOutcome(message=f"Pesanan {order['qty']}x {order['product_name']} dibatalkan")


def build_approval_handlers() -> ApprovalHandlers:
    """The registry the Warung app passes to its ``OfficeContext``."""
    handlers = ApprovalHandlers()
    handlers.register(KIND_PURCHASE_ORDER, PurchaseOrderApproval())
    return handlers
