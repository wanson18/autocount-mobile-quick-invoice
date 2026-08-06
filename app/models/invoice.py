"""Strict Pydantic input models for mobile invoice entry.

These models describe exactly what the mobile client may submit when issuing an
invoice. Any field not declared here is rejected by the ``extra="forbid"`` rule,
so server-side concerns such as account-book IDs, invoice numbers, totals, tax,
PO/reference, salesperson, remarks, free-text lines, PDF fields, and e-Invoice
status fields can never be accepted from the client.

Ownership, date-rule, total, AutoCount availability, and idempotency checks are
deliberately absent: they belong to later services, not to the input layer.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from app.models.company import CompanyKey

NonBlankIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class InvoiceLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: NonBlankIdentifier
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    original_unit_price: Decimal = Field(ge=0)


class InvoiceDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: CompanyKey
    invoice_date: date
    customer_id: NonBlankIdentifier
    delivery_address_id: NonBlankIdentifier
    lines: list[InvoiceLineInput] = Field(min_length=1)
    submit_einvoice: StrictBool = False
    idempotency_key: NonBlankIdentifier


class InvoicePreviewInput(BaseModel):
    """Items to propose historical prices for, before the user confirms.

    Sent by the GPT to obtain the latest issued price per item with its
    source invoice; the proposal is advisory and the confirmed draft carries
    the final prices. No price, quantity, date, or idempotency data is
    accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    company: CompanyKey
    customer_id: NonBlankIdentifier
    item_ids: list[NonBlankIdentifier] = Field(min_length=1)


class PriceOverrideItem(BaseModel):
    item_id: str
    original_unit_price: str
    issued_unit_price: str


class EInvoiceResultItem(BaseModel):
    status: str
    error_message: str | None = None


class InvoiceIssueData(BaseModel):
    company: str
    invoice_id: str
    invoice_number: str
    price_overrides: list[PriceOverrideItem]
    einvoice: EInvoiceResultItem


class InvoiceIssueResponse(BaseModel):
    data: InvoiceIssueData


class PreviewItem(BaseModel):
    item_id: str
    latest_unit_price: str | None = None
    source_invoice_number: str | None = None
    source_invoice_date: str | None = None


class PreviewData(BaseModel):
    customer_id: str
    items: list[PreviewItem]


class PreviewResponse(BaseModel):
    data: PreviewData
