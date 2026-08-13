"""Edit the line set of an already-issued AutoCount invoice.

Deliberately separate from ``app.services.invoice_service``: creation is keyed
by an idempotency key and needs a request repository because a repeated create
makes a second invoice. Editing is keyed by document number, has different
failure modes, and carries no e-Invoice concern.

This module currently holds the editability rule shared by the read and write
paths; the service that applies an edit is added once the live capability
spike confirms AutoCount accepts an update on an approved invoice (see
``docs/specs/2026-08-13-view-edit-invoices-design.md``).
"""

from __future__ import annotations

from datetime import date, timedelta

from app.models.master_data import InvoiceSummary

#: How recent an invoice must be for this app to offer editing. Older
#: invoices are view-only and are corrected in AutoCount directly, which keeps
#: a mistyped edit from reaching an already-reconciled accounting period.
EDIT_WINDOW_DAYS = 30


class InvoiceEditError(Exception):
    """Base error for the invoice edit path."""


class InvoiceNotFoundError(InvoiceEditError):
    """AutoCount has no invoice with the requested document number."""


def is_editable(invoice: InvoiceSummary, *, today: date | None = None) -> bool:
    """Whether this app will offer to edit ``invoice``.

    Cancelled invoices are never editable. Neither is one whose document date
    is more than ``EDIT_WINDOW_DAYS`` old, nor one dated in the future -- a
    future date means the date itself is wrong, not that the invoice is fresh.
    An unparseable document date fails closed as not editable rather than
    raising, so one malformed row cannot break a whole listing.
    """
    if invoice.is_cancelled:
        return False
    today = today or date.today()
    try:
        doc_date = date.fromisoformat(invoice.doc_date[:10])
    except ValueError:
        return False
    if doc_date > today:
        return False
    return (today - doc_date) <= timedelta(days=EDIT_WINDOW_DAYS)
