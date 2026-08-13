"""Normalised read-only master-data representations from AutoCount.

These are the immutable summaries returned by ``AutoCountMasterDataAdapter``.
They intentionally carry no account-book identity: isolation is guaranteed by
the server-side ``CompanyConfig`` passed into every adapter call, never by
these values.

Identity rules:

- A customer's identity is its AutoCount code: ``id == code``.
- A product's identity is its AutoCount lookup code: ``id == code``.
- An invoice's identity is its AutoCount document key (``docKey``).
- ``default_price`` is always an exact ``Decimal``, never a binary float.
"""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel

from app.models.common import ItemResponse, ListResponse


@dataclass(frozen=True)
class CustomerSummary:
    """A customer from the selected account book."""

    id: str
    code: str
    name: str


@dataclass(frozen=True)
class DeliveryAddress:
    """A delivery address owned by one customer.

    ``id`` is a stable adapter ID derived from the owning customer's code, so
    an address object can never be accepted for another customer or account
    book.
    """

    id: str
    label: str
    address_text: str


@dataclass(frozen=True)
class ProductSummary:
    """An item from the selected account book, with its exact master price."""

    id: str
    code: str
    name: str
    default_price: Decimal


@dataclass(frozen=True)
class PriceHistory:
    """The latest issued unit price for one customer/item pair.

    Non-authoritative reference for the confirmation preview: the unit price
    AutoCount last issued to this customer for this item on a non-cancelled
    invoice, plus the source invoice number and date so the preview can be
    traced back to the originating invoice. ``None`` means no prior invoice
    for this pair within the lookup window.
    """

    item_id: str
    customer_id: str
    unit_price: Decimal
    source_invoice_number: str
    source_invoice_date: str


@dataclass(frozen=True)
class InvoiceLineSummary:
    """One detail line of an existing AutoCount invoice.

    ``description`` is what AutoCount stored on the line, kept so a
    line-editing screen can show product names without one master-data call
    per line. It defaults to blank because the listing rows used for price
    history and reconciliation match on code, quantity, and price only.
    """

    product_code: str
    qty: Decimal
    unit_price: Decimal
    description: str = ""


@dataclass(frozen=True)
class InvoiceSummary:
    """An existing AutoCount invoice in the selected account book.

    ``id`` is the AutoCount document key (``docKey`` in the view model).
    ``lines`` is the exact detail multiset AutoCount stored. ``total`` is the
    pre-tax subtotal AutoCount computed. Cancelled/voided invoices are flagged
    with ``is_cancelled`` so price history and reconciliation can exclude them.
    """

    id: str
    doc_no: str
    doc_date: str
    debtor_code: str
    total: Decimal
    lines: tuple[InvoiceLineSummary, ...]
    is_cancelled: bool = False


class CustomerSearchItem(BaseModel):
    id: str
    code: str
    name: str


CustomerSearchResponse = ListResponse[CustomerSearchItem]


class DeliveryAddressItem(BaseModel):
    id: str
    label: str
    address_text: str


DeliveryAddressListResponse = ListResponse[DeliveryAddressItem]


class ProductSearchItem(BaseModel):
    id: str
    code: str
    name: str
    default_price: str


ProductSearchResponse = ListResponse[ProductSearchItem]


class InvoiceLineItem(BaseModel):
    """One line of an issued invoice, with money as exact decimal strings."""

    product_code: str
    description: str
    quantity: str
    unit_price: str


class InvoiceListItem(BaseModel):
    """One row of the recent-invoice browse list."""

    id: str
    doc_no: str
    doc_date: str
    debtor_code: str
    total: str
    is_cancelled: bool
    line_count: int


InvoiceListResponse = ListResponse[InvoiceListItem]


class InvoiceDetailItem(BaseModel):
    """One issued invoice in full.

    ``is_editable`` is the server's answer, not a hint: the client uses it to
    decide what to offer, and the write path re-checks the same rule so a
    caller that ignores it gets rejected anyway.
    """

    id: str
    doc_no: str
    doc_date: str
    debtor_code: str
    total: str
    is_cancelled: bool
    is_editable: bool
    lines: list[InvoiceLineItem]


InvoiceDetailResponse = ItemResponse[InvoiceDetailItem]
