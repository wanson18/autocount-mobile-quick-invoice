"""Create one approved AutoCount invoice from a confirmed mobile draft.

Idempotency and ambiguity:

- The same idempotency key with the same payload returns the stored result.
- A ``pending`` request is never replayed; it needs reconciliation.
- An ``ambiguous`` request (a write that timed out) is reconciled against
  AutoCount before any other action: the adapter searches the account book for
  invoices of the same customer created within a narrow window around the
  ambiguity timestamp, and the draft is matched on customer, date, exact line
  multiset, and non-cancelled status. Exactly one match resolves the request;
  zero matches marks it failed (retry with a new key); several matches require
  manual reconciliation. AutoCount is never called twice to create the same
  invoice.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from app.autocount.errors import AutoCountAmbiguousWriteError, AutoCountDataError
from app.autocount.mapping import map_invoice_payload
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceSummary,
    ProductSummary,
)
from app.repositories.request_repository import (
    InvoiceRequest,
    RequestRepository,
    RequestStatus,
)

#: How wide a creation-time window is searched when reconciling an ambiguous write.
RECONCILE_WINDOW = timedelta(hours=1)


class MasterDataPort(Protocol):
    async def search_customers(
        self, company: CompanyConfig, query: str
    ) -> list[CustomerSummary]: ...

    async def get_delivery_addresses(
        self, company: CompanyConfig, customer_id: str
    ) -> list[DeliveryAddress]: ...

    async def get_item(
        self, company: CompanyConfig, item_id: str
    ) -> ProductSummary: ...

    async def search_invoices(
        self,
        company: CompanyConfig,
        *,
        customer_id: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[InvoiceSummary]: ...


class EInvoicePort(Protocol):
    async def submit(
        self, company: CompanyConfig, invoice_id: str, invoice_number: str
    ) -> Any: ...


class InvoiceServiceError(Exception):
    """Base class for invoice service errors."""


class InvoiceValidationError(InvoiceServiceError):
    """A resolved master-data record does not match the confirmed draft."""


class InvoiceIssuePendingError(InvoiceServiceError):
    """An idempotency key is mid-flight; it must be reconciled before retry."""


class EInvoiceStatus(Enum):
    NOT_REQUESTED = "not_requested"
    SUBMITTED = "submitted"
    ACTION_REQUIRED = "action_required"


@dataclass(frozen=True)
class EInvoiceResult:
    status: EInvoiceStatus
    error_message: str | None = None


@dataclass(frozen=True)
class InvoiceCreateResult:
    invoice_id: str
    invoice_number: str
    einvoice: EInvoiceResult
    price_overrides: tuple[dict[str, Any], ...]


class InvoiceService:
    """Coordinates idempotent, validated invoice creation against AutoCount."""

    def __init__(
        self,
        *,
        company_resolver: Callable[[CompanyKey], CompanyConfig | None],
        master_data: MasterDataPort,
        client: Any,
        requests: RequestRepository,
        einvoice: EInvoicePort | None = None,
    ) -> None:
        self.company_resolver = company_resolver
        self.master_data = master_data
        self.client = client
        self.requests = requests
        self.einvoice = einvoice

    async def issue(self, draft: InvoiceDraftInput) -> InvoiceCreateResult:
        company = self._resolve_company(draft)
        request_hash = self._request_hash(draft)
        request = self.requests.begin(draft.idempotency_key, draft.company, request_hash)

        if not request.is_new:
            if request.status is RequestStatus.SUCCEEDED:
                return self._result_from_request(request, draft)
            if request.status is RequestStatus.AMBIGUOUS:
                return await self._reconcile_ambiguous(draft, company, request)
            if request.status is RequestStatus.PENDING:
                raise InvoiceIssuePendingError(
                    "invoice request is pending; reconcile it before retrying"
                )
            raise InvoiceServiceError(request.error_message or "invoice request failed")

        try:
            customer = await self._resolve_customer(company, draft.customer_id)
            address = await self._resolve_address(
                company, draft.customer_id, draft.delivery_address_id
            )
            products = await self._resolve_products(company, draft)
            accounting_draft = draft.model_copy(update={"submit_einvoice": False})
            payload = map_invoice_payload(accounting_draft, customer, address, products)
            response = await self.client.write(
                company, "POST", "invoice", json=payload
            )
            invoice_id, invoice_number = self._parse_create_response(response)
        except AutoCountAmbiguousWriteError as exc:
            self.requests.mark_ambiguous(draft.idempotency_key, str(exc))
            raise
        except Exception as exc:
            self.requests.mark_failed(draft.idempotency_key, str(exc))
            raise

        return await self._complete_issue(draft, company, invoice_id, invoice_number)

    def _resolve_company(self, draft: InvoiceDraftInput) -> CompanyConfig:
        company = self.company_resolver(draft.company)
        if company is None:
            raise InvoiceValidationError(f"unknown company: {draft.company.value}")
        return company

    async def _resolve_customer(
        self, company: CompanyConfig, customer_id: str
    ) -> CustomerSummary:
        customers = await self.master_data.search_customers(company, customer_id)
        matches = [
            customer
            for customer in customers
            if customer.id == customer_id and customer.code == customer_id
        ]
        if len(matches) != 1:
            raise InvoiceValidationError(
                f"customer {customer_id!r} does not belong to selected company"
            )
        return matches[0]

    async def _resolve_address(
        self, company: CompanyConfig, customer_id: str, address_id: str
    ) -> DeliveryAddress:
        addresses = await self.master_data.get_delivery_addresses(company, customer_id)
        matches = [address for address in addresses if address.id == address_id]
        if len(matches) != 1:
            raise InvoiceValidationError(
                f"delivery address {address_id!r} does not belong to customer {customer_id!r}"
            )
        return matches[0]

    async def _resolve_products(
        self, company: CompanyConfig, draft: InvoiceDraftInput
    ) -> dict[str, ProductSummary]:
        products: dict[str, ProductSummary] = {}
        for line in draft.lines:
            if line.item_id in products:
                continue
            product = await self.master_data.get_item(company, line.item_id)
            if product.id != line.item_id or product.code != line.item_id:
                raise InvoiceValidationError(
                    f"item {line.item_id!r} does not belong to selected company"
                )
            products[line.item_id] = product
        return products

    async def _complete_issue(
        self,
        draft: InvoiceDraftInput,
        company: CompanyConfig,
        invoice_id: str,
        invoice_number: str,
    ) -> InvoiceCreateResult:
        price_overrides = self._price_overrides(draft)
        for override in price_overrides:
            self.requests.record_price_override(
                draft.idempotency_key,
                item_id=override["item_id"],
                original_unit_price=override["original_unit_price"],
                issued_unit_price=override["issued_unit_price"],
                autocount_invoice_id=invoice_id,
            )

        einvoice_result = EInvoiceResult(status=EInvoiceStatus.NOT_REQUESTED)
        if draft.submit_einvoice:
            if self.einvoice is None:
                einvoice_result = EInvoiceResult(
                    status=EInvoiceStatus.ACTION_REQUIRED,
                    error_message="e-Invoice submission is not configured",
                )
            else:
                try:
                    status = await self.einvoice.submit(company, invoice_id, invoice_number)
                    einvoice_result = EInvoiceResult(status=status)
                except Exception as exc:
                    einvoice_result = EInvoiceResult(
                        status=EInvoiceStatus.ACTION_REQUIRED, error_message=str(exc)
                    )

        self.requests.mark_succeeded(
            draft.idempotency_key,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            einvoice_status=einvoice_result.status.value,
            einvoice_error=einvoice_result.error_message,
        )

        return InvoiceCreateResult(
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            einvoice=einvoice_result,
            price_overrides=tuple(price_overrides),
        )

    def _price_overrides(self, draft: InvoiceDraftInput) -> list[dict[str, Any]]:
        overrides = []
        for line in draft.lines:
            if line.unit_price != line.original_unit_price:
                overrides.append(
                    {
                        "item_id": line.item_id,
                        "original_unit_price": line.original_unit_price,
                        "issued_unit_price": line.unit_price,
                    }
                )
        return overrides

    def _request_hash(self, draft: InvoiceDraftInput) -> str:
        payload = draft.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _result_from_request(
        self, request: InvoiceRequest, draft: InvoiceDraftInput
    ) -> InvoiceCreateResult:
        einvoice_status = EInvoiceStatus(request.einvoice_status or EInvoiceStatus.NOT_REQUESTED.value)
        price_overrides = tuple(
            self.requests.list_price_overrides(draft.idempotency_key)
        )
        return InvoiceCreateResult(
            invoice_id=request.invoice_id,
            invoice_number=request.invoice_number,
            einvoice=EInvoiceResult(
                status=einvoice_status, error_message=request.einvoice_error
            ),
            price_overrides=price_overrides,
        )

    def _parse_create_response(self, response: Any) -> tuple[str, str]:
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise AutoCountDataError("AutoCount invoice creation response is missing its data")
        invoice_id = data.get("id")
        invoice_number = data.get("docNo")
        if not invoice_id or not invoice_number:
            raise AutoCountDataError("AutoCount invoice creation response is incomplete")
        return str(invoice_id), str(invoice_number)

    async def _reconcile_ambiguous(
        self, draft: InvoiceDraftInput, company: CompanyConfig, request: InvoiceRequest
    ) -> InvoiceCreateResult:
        window_start = request.created_at - RECONCILE_WINDOW
        window_end = request.created_at + RECONCILE_WINDOW
        candidates = await self.master_data.search_invoices(
            company,
            customer_id=draft.customer_id,
            date_from=window_start,
            date_to=window_end,
        )
        matches = [
            invoice
            for invoice in candidates
            if not invoice.is_cancelled and self._invoice_matches_draft(invoice, draft)
        ]
        if len(matches) == 1:
            invoice = matches[0]
            return await self._complete_issue(draft, company, invoice.id, invoice.doc_no)
        if len(matches) == 0:
            self.requests.mark_failed(
                draft.idempotency_key,
                "ambiguous write did not create an invoice; retry with a new key",
            )
            raise InvoiceServiceError(
                "ambiguous write did not create an invoice; retry with a new key"
            )
        raise InvoiceServiceError(
            f"ambiguous write matched {len(matches)} invoices; manual reconciliation required"
        )

    def _invoice_matches_draft(self, invoice: InvoiceSummary, draft: InvoiceDraftInput) -> bool:
        if invoice.debtor_code != draft.customer_id:
            return False
        if invoice.doc_date != draft.invoice_date.isoformat():
            return False
        expected = Counter(
            (line.item_id, line.quantity, line.unit_price) for line in draft.lines
        )
        actual = Counter(
            (line.product_code, line.qty, line.unit_price) for line in invoice.lines
        )
        return expected == actual
