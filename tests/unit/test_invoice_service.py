"""Task 7 - idempotent invoice creation service."""

from datetime import date
from decimal import Decimal
import hashlib
import json

import pytest

from app.autocount.errors import AutoCountRejectedError
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import CustomerSummary, DeliveryAddress, ProductSummary
from app.repositories.request_repository import RequestRepository, RequestStatus
from app.services.invoice_service import (
    EInvoiceStatus,
    InvoiceIssuePendingError,
    InvoiceService,
    InvoiceValidationError,
)


COMPANY = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id="book-sdn-bhd",
)


class Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeMasterData:
    def __init__(self, *, customer_code="C001"):
        self.customer = CustomerSummary(customer_code, customer_code, "Customer One")
        self.address = DeliveryAddress(
            f"{customer_code}:delivery", "Default delivery address", "1 Main Street"
        )
        self.product = ProductSummary(
            "ITEM-1", "ITEM-1", "Cooking Oil", Decimal("30.00")
        )
        self.customer_code = customer_code
        self.calls = []

    async def search_customers(self, company, query):
        self.calls.append(("customer", company, query))
        return [self.customer] if query.lower() in self.customer.code.lower() else []

    async def get_delivery_addresses(self, company, customer_id):
        self.calls.append(("address", company, customer_id))
        return [self.address]

    async def get_item(self, company, item_id):
        self.calls.append(("item", company, item_id))
        if item_id != self.product.id:
            raise AssertionError(f"unexpected item lookup: {item_id}")
        return self.product

    async def search_invoices(self, company, *, customer_id, date_from, date_to):
        self.calls.append(("invoices", company, customer_id, date_from, date_to))
        return []


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response or Response(
            {"data": {"id": "inv-1", "docNo": "INV-2026-0001"}}
        )
        self.error = error
        self.writes = []

    async def write(self, company, method, endpoint, *, json=None, params=None):
        self.writes.append((company, method, endpoint, json, params))
        if self.error:
            raise self.error
        return self.response


class FakeEInvoice:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def submit(self, company, invoice_id, invoice_number):
        self.calls.append((company, invoice_id, invoice_number))
        if self.error:
            raise self.error
        return EInvoiceStatus.SUBMITTED


def make_draft(*, submit_einvoice=False, customer_id="C001", key="issue-1"):
    return InvoiceDraftInput(
        company=CompanyKey.SDN_BHD,
        invoice_date=date(2026, 8, 4),
        customer_id=customer_id,
        delivery_address_id=f"{customer_id}:delivery",
        lines=[
            {
                "item_id": "ITEM-1",
                "quantity": Decimal("2"),
                "unit_price": Decimal("31.50"),
                "original_unit_price": Decimal("30.00"),
            }
        ],
        submit_einvoice=submit_einvoice,
        idempotency_key=key,
    )


def service(tmp_path, *, client=None, master=None, einvoice=None):
    return InvoiceService(
        company_resolver=lambda key: COMPANY if key is CompanyKey.SDN_BHD else None,
        master_data=master or FakeMasterData(),
        client=client or FakeClient(),
        requests=RequestRepository(tmp_path / "requests.db"),
        einvoice=einvoice,
    )


@pytest.mark.asyncio
async def test_issue_validates_master_data_creates_invoice_and_records_override(tmp_path):
    master = FakeMasterData()
    client = FakeClient()
    invoice_service = service(tmp_path, client=client, master=master)
    result = await invoice_service.issue(make_draft())

    assert result.invoice_id == "inv-1"
    assert result.invoice_number == "INV-2026-0001"
    assert result.einvoice.status is EInvoiceStatus.NOT_REQUESTED
    assert result.price_overrides == (
        {"item_id": "ITEM-1", "original_unit_price": Decimal("30.00"), "issued_unit_price": Decimal("31.50")},
    )
    assert len(client.writes) == 1
    company, method, endpoint, payload, params = client.writes[0]
    assert company is COMPANY
    assert (method, endpoint, params) == ("POST", "invoice", None)
    assert payload["master"]["debtorCode"] == "C001"
    assert payload["master"]["debtorName"] == "Customer One"
    assert payload["master"]["creditTerm"] == "COD"
    assert payload["master"]["salesLocation"] == "HQ"
    assert "accNo" not in payload["master"]
    assert payload["details"][0]["unitPrice"] == Decimal("31.50")
    assert payload["details"][0]["accNo"] == "500-0000"
    assert [call[0] for call in master.calls] == ["customer", "address", "item"]
    assert invoice_service.requests.list_price_overrides("issue-1") == [
        {
            "item_id": "ITEM-1",
            "original_unit_price": Decimal("30.00"),
            "issued_unit_price": Decimal("31.50"),
            "autocount_invoice_id": "inv-1",
        }
    ]


@pytest.mark.asyncio
async def test_replay_same_key_returns_stored_invoice_without_second_create(tmp_path):
    client = FakeClient()
    invoice_service = service(tmp_path, client=client)
    draft = make_draft()

    first = await invoice_service.issue(draft)
    second = await invoice_service.issue(draft)

    assert second.invoice_id == first.invoice_id
    assert second.invoice_number == first.invoice_number
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_existing_pending_request_is_not_replayed_as_second_create(tmp_path):
    repo = RequestRepository(tmp_path / "requests.db")
    draft = make_draft()
    request_hash = hashlib.sha256(
        json.dumps(
            draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    repo.begin(draft.idempotency_key, draft.company, request_hash)

    invoice_service = InvoiceService(
        company_resolver=lambda key: COMPANY,
        master_data=FakeMasterData(),
        client=FakeClient(),
        requests=repo,
    )

    with pytest.raises(InvoiceIssuePendingError, match="pending"):
        await invoice_service.issue(draft)
    assert repo.get(draft.idempotency_key).status is RequestStatus.PENDING


@pytest.mark.asyncio
async def test_customer_ownership_failure_marks_request_failed_without_create(tmp_path):
    client = FakeClient()
    master = FakeMasterData(customer_code="OTHER")
    draft = make_draft()
    invoice_service = service(tmp_path, client=client, master=master)

    with pytest.raises(InvoiceValidationError, match="customer"):
        await invoice_service.issue(draft)

    assert client.writes == []
    assert invoice_service.requests.get(draft.idempotency_key).status is RequestStatus.FAILED


@pytest.mark.asyncio
async def test_autocount_rejection_marks_request_failed_and_does_not_return_success(tmp_path):
    client = FakeClient(error=AutoCountRejectedError(400, "invalid debtor"))
    draft = make_draft()
    invoice_service = service(tmp_path, client=client)

    with pytest.raises(AutoCountRejectedError, match="invalid debtor"):
        await invoice_service.issue(draft)

    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.FAILED
    assert stored.error_message == "invalid debtor"


@pytest.mark.asyncio
async def test_einvoice_failure_is_separate_from_successful_invoice(tmp_path):
    einvoice = FakeEInvoice(error=RuntimeError("taxpayer data incomplete"))
    result = await service(tmp_path, einvoice=einvoice).issue(
        make_draft(submit_einvoice=True)
    )

    assert result.invoice_id == "inv-1"
    assert result.einvoice.status is EInvoiceStatus.ACTION_REQUIRED
    assert result.einvoice.error_message == "taxpayer data incomplete"
    assert len(einvoice.calls) == 1
