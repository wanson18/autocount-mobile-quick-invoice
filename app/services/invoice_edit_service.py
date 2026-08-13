"""Edit the line set of an already-issued AutoCount invoice.

Deliberately separate from ``app.services.invoice_service``: creation is keyed
by an idempotency key and needs a request repository because a repeated create
makes a second invoice. Editing is keyed by document number, has different
failure modes, and carries no e-Invoice concern.

The live spike confirmed the API permits this: an invoice issued with
``saveApprove: true`` accepts a ``PUT`` (204), a shorter ``details`` array
deletes the trailing rows, and the header survives when only the mandatory
``master`` fields are echoed. See ``docs/autocount/invoice-update-spike.md``.

No idempotency keys here. Creation needs them because a repeated create makes
a second invoice; a repeated full-state update re-asserts the same lines, so
this path stores nothing and handles its two write hazards inline.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol, TypeAlias

from app.autocount.errors import AutoCountAmbiguousWriteError, AutoCountRejectedError
from app.autocount.mapping import map_invoice_update_payload
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import ExpectedLine, InvoiceEditInput, InvoiceEditLine
from app.models.master_data import InvoiceLineSummary, InvoiceSummary, ProductSummary

#: A line set reduced to what an edit compares on: product code, quantity,
#: unit price, in order. Descriptions and totals are derived, so they are not
#: part of the comparison.
LineState: TypeAlias = list[tuple[str, Decimal, Decimal]]

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


class InvoiceNotEditableError(InvoiceEditError):
    """The invoice is cancelled or falls outside the edit window."""


class InvoiceChangedError(InvoiceEditError):
    """The invoice changed in AutoCount since the client loaded it."""


class InvoiceEditUnconfirmedError(InvoiceEditError):
    """A timed-out edit could not be confirmed by re-reading the invoice."""


class EditMasterDataPort(Protocol):
    async def get_invoice(
        self, company: CompanyConfig, invoice_no: str
    ) -> InvoiceSummary: ...

    async def get_item(
        self, company: CompanyConfig, item_id: str
    ) -> ProductSummary: ...


class EditWritePort(Protocol):
    async def write(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any: ...


class InvoiceEditService:
    """Apply one edited line set to an already-issued AutoCount invoice.

    Two write hazards, both handled without any stored state:

    - **Stale write.** The client sends the line set it loaded; the invoice is
      re-read immediately before the update and the edit is refused if it no
      longer matches, rather than silently clobbering a change made in
      AutoCount in the meantime.
    - **Ambiguous write.** A timeout leaves the update's fate unknown, but
      because the request carried absolute desired state the answer is one
      re-read away: the stored invoice either matches what was asked for or it
      does not. The update is never retried.

    The single pre-write read serves three purposes at once -- the editability
    check, the stale-write comparison, and the ``master`` fields the update
    must echo back.
    """

    def __init__(
        self,
        *,
        company_resolver: Callable[[CompanyKey], CompanyConfig | None],
        master_data: EditMasterDataPort,
        client: EditWritePort,
    ) -> None:
        self.company_resolver = company_resolver
        self.master_data = master_data
        self.client = client

    async def edit(
        self,
        doc_no: str,
        edit: InvoiceEditInput,
        *,
        today: date | None = None,
    ) -> InvoiceSummary:
        company = self.company_resolver(edit.company)
        if company is None:
            raise InvoiceEditError(f"unknown company: {edit.company.value}")

        current = await self._read(company, doc_no)
        if not is_editable(current, today=today):
            raise InvoiceNotEditableError(
                f"invoice {doc_no} is cancelled or more than {EDIT_WINDOW_DAYS} "
                "days old, and can only be corrected in AutoCount"
            )
        self._assert_unchanged(current, edit, doc_no)

        products = await self._resolve_products(company, edit)
        try:
            payload = map_invoice_update_payload(current, edit.lines, products)
        except ValueError as exc:
            raise InvoiceEditError(str(exc)) from None

        try:
            await self.client.write(
                company, "PUT", "invoice", params={"docNo": doc_no}, json=payload
            )
        except AutoCountAmbiguousWriteError as ambiguous:
            return await self._reconcile(company, doc_no, edit, ambiguous)

        return await self._read(company, doc_no)

    async def _read(self, company: CompanyConfig, doc_no: str) -> InvoiceSummary:
        """Fetch one invoice, turning AutoCount's 404 into a domain error.

        Any other upstream status keeps propagating so a real failure is not
        mistaken for a missing invoice.
        """
        try:
            return await self.master_data.get_invoice(company, doc_no)
        except AutoCountRejectedError as exc:
            if exc.status_code == 404:
                raise InvoiceNotFoundError(
                    f"no invoice {doc_no!r} in the selected company"
                ) from None
            raise

    @staticmethod
    def _stored_state(lines: Sequence[InvoiceLineSummary]) -> LineState:
        """AutoCount's line set, in comparable form."""
        return [(line.product_code, line.qty, line.unit_price) for line in lines]

    @staticmethod
    def _requested_state(lines: Sequence[InvoiceEditLine | ExpectedLine]) -> LineState:
        """A client's line set, in the same comparable form.

        ``item_id`` is AutoCount's product code, so the two shapes line up
        without a lookup.
        """
        return [(line.item_id, line.quantity, line.unit_price) for line in lines]

    @classmethod
    def _assert_unchanged(
        cls, current: InvoiceSummary, edit: InvoiceEditInput, doc_no: str
    ) -> None:
        """Refuse a save built on a stale view of the invoice.

        Compared in order, because row position is meaningful to AutoCount and
        a reorder is a real change, and with exact ``Decimal`` equality rather
        than any tolerance -- money differing at all is money differing.
        """
        if cls._requested_state(edit.expected_lines) != cls._stored_state(current.lines):
            raise InvoiceChangedError(
                f"invoice {doc_no} changed in AutoCount since it was opened; "
                "reopen it and reapply the edit"
            )

    async def _resolve_products(
        self, company: CompanyConfig, edit: InvoiceEditInput
    ) -> dict[str, ProductSummary]:
        """Resolve every edited line's product from the selected account book.

        Mirrors the create path: an item is only usable if this company's own
        book returns it under exactly that code, so an edit can never
        introduce an item belonging to the other company.
        """
        products: dict[str, ProductSummary] = {}
        for line in edit.lines:
            if line.item_id in products:
                continue
            product = await self.master_data.get_item(company, line.item_id)
            if product.id != line.item_id or product.code != line.item_id:
                raise InvoiceEditError(
                    f"item {line.item_id!r} does not belong to selected company"
                )
            products[line.item_id] = product
        return products

    async def _reconcile(
        self,
        company: CompanyConfig,
        doc_no: str,
        edit: InvoiceEditInput,
        ambiguous: AutoCountAmbiguousWriteError,
    ) -> InvoiceSummary:
        """Decide whether a timed-out update actually applied.

        The update carried absolute desired state, so the stored invoice
        either matches it or it does not; there is no partial application to
        unpick. When the re-read itself fails there is no answer available, so
        the original ambiguity propagates and the caller sees the existing
        retryable 502 rather than a confident "it did not apply".
        """
        try:
            current = await self._read(company, doc_no)
        except (InvoiceEditError, AutoCountRejectedError):
            raise ambiguous from None

        if self._requested_state(edit.lines) == self._stored_state(current.lines):
            return current
        raise InvoiceEditUnconfirmedError(
            f"the edit to invoice {doc_no} timed out and could not be confirmed; "
            "reopen the invoice to check its current state before retrying"
        )
