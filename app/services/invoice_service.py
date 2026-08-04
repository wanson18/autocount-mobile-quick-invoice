"""Create one approved AutoCount invoice from a confirmed mobile draft."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from app.autocount.errors import AutoCountAmbiguousWriteError, AutoCountDataError
from app.autocount.mapping import map_invoice_payload
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import CustomerSummary, DeliveryAddress, ProductSummary
from app.repositories.request_repository import (
    InvoiceRequest,
    RequestRepository,
    RequestStatus,
)


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


class EInvoicePort(Protocol):
    async def submit(
        self, company: CompanyConfig, invoice_id: str, invoice_number: str
    ) -> str | "EInvoiceStatus": ...


class InvoiceServiceError(Exception):
    """Base error for invoice service failures."""


class InvoiceValidationError(InvoiceServiceError):
    """The selected master data or confirmed draft is not issuable."""


class InvoiceIssuePendingError(InvoiceServiceError):
    """An earlier request owns this idempotency key and needs reconciliation."""


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
            if request.status in {RequestStatus.PENDING, RequestStatus.AMBIGUOUS}:
                raise InvoiceIssuePendingError(
                    f"invoice request is {request.status.value}; reconcile it before retrying"
                )
            raise InvoiceServiceError(request.error_message or "invoice request failed")

        try:
            await self._resolve_customer(company, draft.customer_id)
            address = await self._resolve_address(
                company, draft.customer_id, draft.delivery_address_id
            )
            products = await self._resolve_products(company, draft)
            accounting_draft = draft.model_copy(update={"submit_einvoice": False})
            payload = map_invoice_payload(accounting_draft, address, products)
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

        self.requests.mark_succeeded(draft.idempotency_key, invoice_id, invoice_number)
        price_overrides = self._price_overrides(draft)
        self.requests.record_price_overrides(
            draft.idempotency_key, invoice_id, price_overrides
        )
        einvoice_result = await self._request_einvoice(
            draft, company, invoice_id, invoice_number
        )
        return self._result(
            draft,
            invoice_id,
            invoice_number,
            price_overrides,
            einvoice_result,
        )

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

    async def _request_einvoice(
        self,
        draft: InvoiceDraftInput,
        company: CompanyConfig,
        invoice_id: str,
        invoice_number: str,
    ) -> EInvoiceResult:
        if not draft.submit_einvoice:
            return EInvoiceResult(EInvoiceStatus.NOT_REQUESTED)
        if self.einvoice is None:
            return EInvoiceResult(
                EInvoiceStatus.UNSUPPORTED,
                "AutoCount e-Invoice processor is not configured",
            )
        try:
            status = await self.einvoice.submit(company, invoice_id, invoice_number)
            return EInvoiceResult(EInvoiceStatus(status))
        except Exception as exc:
            return EInvoiceResult(EInvoiceStatus.ACTION_REQUIRED, str(exc))

    @staticmethod
    def _request_hash(draft: InvoiceDraftInput) -> str:
        canonical = json.dumps(
            draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _parse_create_response(response: Any) -> tuple[str, str]:
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            raise AutoCountDataError(
                "AutoCount invoice create response is not valid JSON"
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise AutoCountDataError(
                "AutoCount invoice create response is missing its data object"
            )
        data = payload["data"]
        invoice_id = data.get("id")
        invoice_number = data.get("docNo")
        if not _non_blank_string(invoice_id) or not _non_blank_string(invoice_number):
            raise AutoCountDataError(
                "AutoCount invoice create response is missing invoice identity"
            )
        return invoice_id.strip(), invoice_number.strip()

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
