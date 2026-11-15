"""What the owner's decision does to a large Jasa invoice (task 14.2).

The billing agent leaves an invoice above ``auto_approve_limit_idr`` as ``draft`` and files a request of kind
``invoice`` (see ``roles.py``). This module is the handler the app registers in the skeleton's
:class:`ApprovalHandlers`.

* **Approve:** ``approve_invoice(..., approved_by='owner')``. An invoice is a claim on a customer, not money the
  business spends, so no ledger row is written and the daily spending limit is not involved. After this the
  database triggers make the invoice immutable.
* **Reject:** ``void_invoice``: the draft is voided and the billing agent does not invoice that booking again
  (a voided invoice still counts as "already invoiced").

Depends only on ``skeleton.*`` and the other Jasa modules; the skeleton never imports this module.
"""
from __future__ import annotations

import sqlite3

from apps.kantor_jasa import data
from apps.kantor_jasa.roles import KIND_INVOICE, rupiah
from skeleton.core.clock import Clock
from skeleton.policy.approvals import ApprovalFailed, ApprovalHandlers, HandlerOutcome


def _draft_of(conn: sqlite3.Connection, approval: dict) -> dict:
    invoice_id = approval["detail"].get("invoice_id")
    invoice = data.get_invoice(conn, approval["business_id"], invoice_id) if isinstance(invoice_id, int) else None
    if invoice is None:
        raise ApprovalFailed("Invoice tidak ditemukan")
    if invoice["status"] != "draft":
        raise ApprovalFailed(f"Invoice #{invoice['id']} sudah diproses")
    return invoice


class InvoiceApproval:
    """Approve or void a draft invoice. Runs inside the decision's transaction."""

    def approve(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome:
        invoice = _draft_of(conn, approval)
        try:
            data.approve_invoice(conn, approval["business_id"], invoice["id"], "owner", clock)
        except data.JasaError as exc:
            raise ApprovalFailed(f"Invoice #{invoice['id']} tidak bisa disetujui: {exc}") from exc
        return HandlerOutcome(message=f"Invoice #{invoice['id']} {rupiah(invoice['amount_idr'])} disetujui pemilik")

    def reject(self, conn: sqlite3.Connection, approval: dict, clock: Clock) -> HandlerOutcome:
        invoice = _draft_of(conn, approval)
        try:
            data.void_invoice(conn, approval["business_id"], invoice["id"])
        except data.JasaError as exc:
            raise ApprovalFailed(f"Invoice #{invoice['id']} tidak bisa dibatalkan: {exc}") from exc
        return HandlerOutcome(message=f"Invoice #{invoice['id']} dibatalkan")


def build_approval_handlers() -> ApprovalHandlers:
    """The registry the Jasa app passes to its ``OfficeContext``."""
    handlers = ApprovalHandlers()
    handlers.register(KIND_INVOICE, InvoiceApproval())
    return handlers
