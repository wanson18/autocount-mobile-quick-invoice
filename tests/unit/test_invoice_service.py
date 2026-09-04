"""Task 7 - idempotent invoice creation service."""

from datetime import date
from decimal import Decimal
import hashlib
import json

import pytest

from app.autocount.errors import AutoCountDataError, AutoCountRejectedError
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
    def __init__(self, payload=None, *, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeMasterData:
    def __init__(self, *, customer_code="C001"):
        self.customer = CustomerSummary(
            customer_code,
            customer_code,
            "Customer One",
            tax_entity="TIN:C23453889090",
        )
        self.address = DeliveryAddress(
            f"{customer_code}:delivery", "Default delivery address", "1 Main Street"
        )
        self.product = ProductSummary(
            "ITEM-1",
            "ITEM-1",
            "Cooking Oil",
            Decimal("30.00"),
            classification_code="022",
        )
        self.customer_code = customer_code
        self.calls = []

    async def search_customers(self, company, query):
        self.calls.append(("customer", company, query))
        return [self.customer] if query.lower() in self.customer.code.lower() else []

    async def get_customer(self, company, customer_id):
        self.calls.append(("customer_detail", company, customer_id))
        return self.customer

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
    """Mirrors AutoCount's real Create Invoice contract: POST returns a bare
    201 with only a ``location`` header (no JSON body), and the invoice
    identity is recovered via a follow-up GET, exactly like
    ``InvoiceService._resolve_created_invoice`` does against the live API.
    """

    def __init__(self, *, write_response=None, read_response=None, error=None):
        self.write_response = write_response or Response(
            headers={"location": "https://accounting-api.autocountcloud.com/1/invoice?docNo=INV-2026-0001"}
        )
        self.read_response = read_response or Response(
            {"master": {"docKey": "inv-1", "docNo": "INV-2026-0001"}}
        )
        self.error = error
        self.writes = []
        self.reads = []

    async def write(self, company, method, endpoint, *, json=None, params=None):
        self.writes.append((company, method, endpoint, json, params))
        if self.error:
            raise self.error
        return self.write_response

    async def read(self, company, method, endpoint, *, json=None, params=None):
        self.reads.append((company, method, endpoint, json, params))
        return self.read_response


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


def service(tmp_path, *, client=None, master=None):
    return InvoiceService(
        company_resolver=lambda key: COMPANY if key is CompanyKey.SDN_BHD else None,
        master_data=master or FakeMasterData(),
        client=client or FakeClient(),
        requests=RequestRepository(tmp_path / "requests.db"),
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
    assert payload["master"]["creditTerm"] == "C.O.D."
    assert payload["master"]["salesLocation"] == "HQ"
    assert payload["master"]["paymentMethod"] == "CASH"
    assert payload["master"]["taxEntity"] == "TIN:C23453889090"
    assert "accNo" not in payload["master"]
    assert payload["details"][0]["unitPrice"] == Decimal("31.50")
    assert payload["details"][0]["accNo"] == "500-0000"
    assert payload["details"][0]["classificationCode"] == "022"
    assert [call[0] for call in master.calls] == ["customer_detail", "address", "item"]
    assert len(client.reads) == 1
    read_company, read_method, read_endpoint, _, read_params = client.reads[0]
    assert read_company is COMPANY
    assert (read_method, read_endpoint, read_params) == (
        "GET",
        "invoice",
        {"docNo": "INV-2026-0001"},
    )
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
async def test_einvoice_request_is_embedded_in_autocount_create(tmp_path):
    client = FakeClient()
    result = await service(tmp_path, client=client).issue(
        make_draft(submit_einvoice=True)
    )

    assert result.invoice_id == "inv-1"
    assert result.einvoice.status is EInvoiceStatus.PENDING
    assert result.einvoice.error_message is None
    assert client.writes[0][3]["master"]["submitEInvoice"] is True


@pytest.mark.asyncio
async def test_create_response_missing_location_header_fails_closed(tmp_path):
    """AutoCount's documented Create Invoice success has no guaranteed JSON
    body -- only a ``location`` header. If that header is absent, the
    request must fail closed rather than silently return no identity."""
    client = FakeClient(write_response=Response(headers={}))
    draft = make_draft()
    invoice_service = service(tmp_path, client=client)

    with pytest.raises(AutoCountDataError, match="location header"):
        await invoice_service.issue(draft)

    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.FAILED
    assert client.reads == []


@pytest.mark.asyncio
async def test_create_response_location_header_without_docno_fails_closed(tmp_path):
    client = FakeClient(
        write_response=Response(
            headers={"location": "https://accounting-api.autocountcloud.com/1/invoice"}
        )
    )
    draft = make_draft()
    invoice_service = service(tmp_path, client=client)

    with pytest.raises(AutoCountDataError, match="docNo"):
        await invoice_service.issue(draft)


@pytest.mark.asyncio
async def test_get_invoice_response_missing_master_fails_closed(tmp_path):
    client = FakeClient(read_response=Response({"details": []}))
    draft = make_draft()
    invoice_service = service(tmp_path, client=client)

    with pytest.raises(AutoCountDataError, match="master"):
        await invoice_service.issue(draft)
