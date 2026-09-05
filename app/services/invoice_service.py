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
from urllib.parse import parse_qs, urlsplit

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
    RequestRepositoryPort,
    RequestStatus,
)
from app.services.product_resolution import (
    ProductNotInCompanyError,
    resolve_products,
)

#: How wide a creation-time window is searched when reconciling an ambiguous write.
RECONCILE_WINDOW = timedelta(hours=1)


class MasterDataPort(Protocol):
    async def get_customer(
        self, company: CompanyConfig, customer_id: str
    ) -> CustomerSummary: ...

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
        date_from: str,
        date_to: str,
    ) -> list[InvoiceSummary]: ...


class AutoCountWritePort(Protocol):
    async def write(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any: ...

    async def read(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any: ...


class InvoiceServiceError(Exception):
    """Base error for invoice service failures."""


class InvoiceValidationError(InvoiceServiceError):
    """The selected master data or confirmed draft is not issuable."""


class InvoiceIssuePendingError(InvoiceServiceError):
    """An earlier request owns this idempotency key and needs reconciliation."""


class InvoiceReconciliationError(InvoiceServiceError):
    """An ambiguous write could not be resolved to exactly one AutoCount invoice.

    ``candidates`` lists the matching AutoCount document numbers when several
    invoices match the draft; it is empty when none matched. Zero matches means
    the write was not applied and a new idempotency key is required; several
    matches require manual reconciliation inside AutoCount.
    """

    def __init__(self, message: str, *, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


class EInvoiceStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACTION_REQUIRED = "action_required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class EInvoiceResult:
    status: EInvoiceStatus
    error_message: str | None = None


@dataclass(frozen=True)
class InvoiceCreateResult:
    company: CompanyKey
    invoice_id: str
    invoice_number: str
    price_overrides: tuple[dict[str, Any], ...]
    einvoice: EInvoiceResult


class InvoiceService:
    """Validate, create, and record one idempotent AutoCount invoice."""

    def __init__(
        self,
        *,
        company_resolver: Callable[[CompanyKey], CompanyConfig | None],
        master_data: MasterDataPort,
        client: AutoCountWritePort,
        requests: RequestRepositoryPort,
    ) -> None:
        self.company_resolver = company_resolver
        self.master_data = master_data
        self.client = client
        self.requests = requests

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
            payload = map_invoice_payload(draft, customer, address, products)
            response = await self.client.write(
                company, "POST", "invoice", json=payload
            )
            invoice_id, invoice_number = await self._resolve_created_invoice(
                company, response
            )
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
        customer = await self.master_data.get_customer(company, customer_id)
        if customer.id != customer_id or customer.code != customer_id:
            raise InvoiceValidationError(
                f"customer {customer_id!r} does not belong to selected company"
            )
        return customer

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
        try:
            return await resolve_products(self.master_data, company, draft.lines)
        except ProductNotInCompanyError as exc:
            raise InvoiceValidationError(str(exc)) from None

    async def _complete_issue(
        self,
        draft: InvoiceDraftInput,
        company: CompanyConfig,
        invoice_id: str,
        invoice_number: str,
    ) -> InvoiceCreateResult:
        """Record a confirmed AutoCount invoice and produce the result.

        Shared by the direct create path and the reconciliation path so both
        mark success, store price overrides, and request e-Invoice processing
        in exactly the same order.
        """
        self.requests.mark_succeeded(draft.idempotency_key, invoice_id, invoice_number)
        price_overrides = self._price_overrides(draft)
        self.requests.record_price_overrides(
            draft.idempotency_key, invoice_id, price_overrides
        )
        einvoice_result = await self._request_einvoice(draft)
        return self._result(
            draft,
            invoice_id,
            invoice_number,
            price_overrides,
            einvoice_result,
        )

    async def _reconcile_ambiguous(
        self,
        draft: InvoiceDraftInput,
        company: CompanyConfig,
        request: InvoiceRequest,
    ) -> InvoiceCreateResult:
        """Resolve a timed-out write against AutoCount before anything else.

        Searches the account book for invoices of the same customer created
        within ``RECONCILE_WINDOW`` around the moment the ambiguity was
        recorded (``request.updated_at``), then keeps only invoices matching
        the confirmed draft exactly. A single match completes the request and
        is returned without ever repeating the create call; zero matches marks
        the request failed so the caller can retry with a new idempotency key;
        several matches require manual reconciliation and are reported with
        their document numbers.
        """
        try:
            ambiguous_at = datetime.fromisoformat(request.updated_at)
        except ValueError:
            raise InvoiceReconciliationError(
                "stored ambiguous request has an unreadable timestamp"
            ) from None
        candidates = [
            invoice
            for invoice in await self.master_data.search_invoices(
                company,
                customer_id=draft.customer_id,
                date_from=(ambiguous_at - RECONCILE_WINDOW).isoformat(),
                date_to=(ambiguous_at + RECONCILE_WINDOW).isoformat(),
            )
            if self._invoice_matches(invoice, draft)
        ]
        if len(candidates) == 1:
            invoice = candidates[0]
            return await self._complete_issue(
                draft, company, invoice.id, invoice.doc_no
            )
        if not candidates:
            message = (
                "no AutoCount invoice matching the draft was found; the timed-out "
                "write was not applied, retry with a new idempotency key"
            )
            self.requests.mark_failed(draft.idempotency_key, message)
            raise InvoiceReconciliationError(message)
        raise InvoiceReconciliationError(
            "multiple AutoCount invoices match the draft; manual reconciliation is required",
            candidates=tuple(invoice.doc_no for invoice in candidates),
        )

    @staticmethod
    def _invoice_matches(
        invoice: InvoiceSummary, draft: InvoiceDraftInput
    ) -> bool:
        """True when AutoCount's invoice equals the confirmed draft exactly.

        Compares the customer, the document date, the cancelled flag, and the
        full detail multiset (product code, quantity, unit price). A multiset
        comparison absorbs AutoCount line ordering, and exact line equality
        also fixes the pre-tax subtotal, so no separate total check is needed.
        """
        if invoice.is_cancelled:
            return False
        if invoice.debtor_code != draft.customer_id:
            return False
        if invoice.doc_date != draft.invoice_date.isoformat():
            return False
        if len(invoice.lines) != len(draft.lines):
            return False
        expected = Counter(
            (line.item_id, line.quantity, line.unit_price) for line in draft.lines
        )
        actual = Counter(
            (line.product_code, line.qty, line.unit_price)
            for line in invoice.lines
        )
        return expected == actual

    async def _request_einvoice(
        self, draft: InvoiceDraftInput
    ) -> EInvoiceResult:
        if not draft.submit_einvoice:
            return EInvoiceResult(EInvoiceStatus.NOT_REQUESTED)
        # ``submitEInvoice`` is sent in the same AutoCount Create Invoice
        # request. Do not call the legacy injected port here: doing so would
        # submit the same invoice twice. AutoCount may validate asynchronously,
        # so the result is pending until its invoice view is read back.
        return EInvoiceResult(EInvoiceStatus.PENDING)

    @staticmethod
    def _request_hash(draft: InvoiceDraftInput) -> str:
        canonical = json.dumps(
            draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _resolve_created_invoice(
        self, company: CompanyConfig, create_response: Any
    ) -> tuple[str, str]:
        """Resolve ``(invoice_id, invoice_number)`` for a just-created invoice.

        AutoCount's documented Create Invoice success response is a bare 201
        with no guaranteed JSON body -- the only documented success signal is
        the ``location`` response header, described as "the link to the
        created record, similar to request url in Get Invoice API method"
        (https://accounting-api.autocountcloud.com/documentation/api-methods/invoice/create-invoice/).
        Get Invoice's URL shape is ``.../invoice?docNo=<docNo>`` (see
        https://accounting-api.autocountcloud.com/documentation/api-methods/invoice/get-invoice/),
        so the created invoice's ``docNo`` is recovered from that header's
        query string. The invoice's true identity, ``docKey`` (see
        https://accounting-api.autocountcloud.com/documentation/models/invoice/viewmodels/invoice-master-viewmodel/),
        is not in that header, so a follow-up Get Invoice call fetches it.
        """
        doc_no = self._doc_no_from_location(create_response)
        get_response = await self.client.read(
            company, "GET", "invoice", params={"docNo": doc_no}
        )
        return self._parse_invoice_view(get_response)

    @staticmethod
    def _doc_no_from_location(response: Any) -> str:
        location = getattr(response, "headers", {}).get("location")
        if not _non_blank_string(location):
            raise AutoCountDataError(
                "AutoCount invoice create response is missing a location header"
            )
        query = urlsplit(location).query
        doc_no = parse_qs(query).get("docNo", [None])[0]
        if not _non_blank_string(doc_no):
            raise AutoCountDataError(
                "AutoCount invoice create response location header has no docNo"
            )
        return doc_no.strip()

    @staticmethod
    def _parse_invoice_view(response: Any) -> tuple[str, str]:
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            raise AutoCountDataError(
                "AutoCount get-invoice response is not valid JSON"
            ) from None
        master = payload.get("master") if isinstance(payload, dict) else None
        if not isinstance(master, dict):
            raise AutoCountDataError(
                "AutoCount get-invoice response is missing its master object"
            )
        doc_key = master.get("docKey")
        doc_no = master.get("docNo")
        if doc_key is None or not _non_blank_string(doc_no):
            raise AutoCountDataError(
                "AutoCount get-invoice response is missing invoice identity"
            )
        return str(doc_key).strip(), doc_no.strip()

    @staticmethod
    def _price_overrides(draft: InvoiceDraftInput) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "item_id": line.item_id,
                "original_unit_price": line.original_unit_price,
                "issued_unit_price": line.unit_price,
            }
            for line in draft.lines
            if line.original_unit_price != line.unit_price
        )

    def _result_from_request(
        self, request: InvoiceRequest, draft: InvoiceDraftInput
    ) -> InvoiceCreateResult:
        if not request.autocount_invoice_id or not request.autocount_invoice_number:
            raise InvoiceServiceError("stored successful request is missing invoice identity")
        return self._result(
            draft,
            request.autocount_invoice_id,
            request.autocount_invoice_number,
            self._price_overrides(draft),
            EInvoiceResult(
                EInvoiceStatus.NOT_REQUESTED
                if not draft.submit_einvoice
                else EInvoiceStatus.PENDING,
                "stored invoice e-Invoice state requires read-back"
                if draft.submit_einvoice
                else None,
            ),
        )

    @staticmethod
    def _result(
        draft: InvoiceDraftInput,
        invoice_id: str,
        invoice_number: str,
        price_overrides: tuple[dict[str, Any], ...],
        einvoice: EInvoiceResult,
    ) -> InvoiceCreateResult:
        return InvoiceCreateResult(
            company=draft.company,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            price_overrides=price_overrides,
            einvoice=einvoice,
        )


def _non_blank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
