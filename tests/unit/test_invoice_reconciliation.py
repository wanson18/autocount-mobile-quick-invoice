"""Task 8 - ambiguous write reconciliation.

Proves that a timed-out create is never blindly replayed: the second tap with
the same idempotency key searches AutoCount for the invoice, resolves it when
exactly one match exists (no duplicate create), marks it failed when no match
exists (so a new idempotency key is required), and demands manual
reconciliation when several invoices match.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.autocount.errors import AutoCountAmbiguousWriteError
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceLineSummary,
    InvoiceSummary,
    ProductSummary,
)
from app.repositories.request_repository import RequestRepository, RequestStatus
from app.services.invoice_service import (
    EInvoiceStatus,
    InvoiceReconciliationError,
    InvoiceService,
    InvoiceServiceError,
)

COMPANY = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id="book-sdn-bhd",
)

INVOICE_DATE = date(2026, 8, 4)
DOC_DATE = INVOICE_DATE.isoformat()


class Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeMasterData:
    def __init__(self, *, invoices=()):
        self.customer = CustomerSummary("C001", "C001", "Customer One")
        self.address = DeliveryAddress(
            "C001:delivery", "Default delivery address", "1 Main Street"
        )
        self.product = ProductSummary(
            "ITEM-1", "ITEM-1", "Cooking Oil", Decimal("30.00")
        )
        self.invoices = list(invoices)
        self.calls = []
        self.invoice_searches = []

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
        self.invoice_searches.append((company, customer_id, date_from, date_to))
        return self.invoices


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.writes = []

    async def write(self, company, method, endpoint, *, json=None, params=None):
        self.writes.append((company, method, endpoint, json, params))
        if self.error:
            raise self.error
        return Response({"data": {"id": "inv-1", "docNo": "INV-2026-0001"}})


def make_draft(*, submit_einvoice=False, key="issue-1"):
    return InvoiceDraftInput(
        company=CompanyKey.SDN_BHD,
        invoice_date=INVOICE_DATE,
        customer_id="C001",
        delivery_address_id="C001:delivery",
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


def matching_invoice(
    *,
    doc_key="9001",
    doc_no="INV-2026-0001",
    qty=Decimal("2"),
    unit_price=Decimal("31.50"),
    is_cancelled=False,
):
    return InvoiceSummary(
        id=doc_key,
        doc_no=doc_no,
        doc_date=DOC_DATE,
        debtor_code="C001",
        total=qty * unit_price,
        lines=(
            InvoiceLineSummary(
                product_code="ITEM-1", qty=qty, unit_price=unit_price
            ),
        ),
        is_cancelled=is_cancelled,
    )


def service(tmp_path, *, master=None, client=None):
    return InvoiceService(
        company_resolver=lambda key: COMPANY if key is CompanyKey.SDN_BHD else None,
        master_data=master or FakeMasterData(),
        client=client or FakeClient(),
        requests=RequestRepository(tmp_path / "requests.db"),
    )


async def ambiguous_first_tap(invoice_service, draft):
    """First tap times out and leaves the request ambiguous; raises it."""
    with pytest.raises(AutoCountAmbiguousWriteError):
        await invoice_service.issue(draft)
    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.AMBIGUOUS
    return stored


@pytest.mark.asyncio
async def test_reconcile_single_match_returns_stored_result_without_second_create(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(invoices=[matching_invoice()])
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    result = await invoice_service.issue(draft)

    assert result.invoice_id == "9001"
    assert result.invoice_number == "INV-2026-0001"
    assert result.einvoice.status is EInvoiceStatus.NOT_REQUESTED
    assert len(client.writes) == 1
    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.SUCCEEDED
    assert stored.autocount_invoice_id == "9001"
    assert stored.autocount_invoice_number == "INV-2026-0001"


@pytest.mark.asyncio
async def test_reconcile_records_price_overrides(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(invoices=[matching_invoice()])
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    result = await invoice_service.issue(draft)

    assert result.price_overrides == (
        {
            "item_id": "ITEM-1",
            "original_unit_price": Decimal("30.00"),
            "issued_unit_price": Decimal("31.50"),
        },
    )
    assert invoice_service.requests.list_price_overrides(draft.idempotency_key) == [
        {
            "item_id": "ITEM-1",
            "original_unit_price": Decimal("30.00"),
            "issued_unit_price": Decimal("31.50"),
            "autocount_invoice_id": "9001",
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_no_match_marks_failed_and_requires_new_key(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    invoice_service = service(tmp_path, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    with pytest.raises(InvoiceReconciliationError, match="not applied") as exc_info:
        await invoice_service.issue(draft)

    assert exc_info.value.candidates == ()
    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.FAILED
    assert len(client.writes) == 1
    # A further tap with the same key surfaces the stored failure, never a create.
    with pytest.raises(InvoiceServiceError, match="not applied"):
        await invoice_service.issue(draft)
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_reconcile_multiple_candidates_require_manual(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(
        invoices=[
            matching_invoice(doc_key="9001", doc_no="INV-2026-0001"),
            matching_invoice(doc_key="9002", doc_no="INV-2026-0002"),
        ]
    )
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    with pytest.raises(InvoiceReconciliationError, match="manual reconciliation") as exc_info:
        await invoice_service.issue(draft)

    assert exc_info.value.candidates == ("INV-2026-0001", "INV-2026-0002")
    stored = invoice_service.requests.get(draft.idempotency_key)
    assert stored.status is RequestStatus.AMBIGUOUS
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_reconcile_line_mismatch_not_counted_as_match(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(
        invoices=[matching_invoice(qty=Decimal("3"), doc_no="INV-2026-0001")]
    )
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    with pytest.raises(InvoiceReconciliationError, match="not applied"):
        await invoice_service.issue(draft)

    assert invoice_service.requests.get(draft.idempotency_key).status is RequestStatus.FAILED


@pytest.mark.asyncio
async def test_reconcile_skips_cancelled_invoice(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(
        invoices=[matching_invoice(is_cancelled=True, doc_no="INV-2026-0001")]
    )
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    await ambiguous_first_tap(invoice_service, draft)
    with pytest.raises(InvoiceReconciliationError, match="not applied"):
        await invoice_service.issue(draft)

    assert invoice_service.requests.get(draft.idempotency_key).status is RequestStatus.FAILED


@pytest.mark.asyncio
async def test_reconcile_searches_window_around_ambiguity_timestamp(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(invoices=[matching_invoice()])
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    stored = await ambiguous_first_tap(invoice_service, draft)
    ambiguous_at = datetime.fromisoformat(stored.updated_at)
    await invoice_service.issue(draft)

    assert len(master.invoice_searches) == 1
    company, customer_id, date_from, date_to = master.invoice_searches[0]
    assert company is COMPANY
    assert customer_id == "C001"
    assert date_from == (ambiguous_at - timedelta(hours=1)).isoformat()
    assert date_to == (ambiguous_at + timedelta(hours=1)).isoformat()


@pytest.mark.asyncio
async def test_reconcile_requests_einvoice_for_resolved_invoice(tmp_path):
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(invoices=[matching_invoice(doc_key="9001", doc_no="INV-2026-0001")])
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft(submit_einvoice=True)

    await ambiguous_first_tap(invoice_service, draft)
    result = await invoice_service.issue(draft)

    assert result.invoice_id == "9001"
    assert result.einvoice.status is EInvoiceStatus.PENDING


@pytest.mark.asyncio
async def test_ambiguous_write_error_triggers_reconciliation_on_next_tap(tmp_path):
    """End to end: exactly one create call, second tap resolves via AutoCount."""
    client = FakeClient(error=AutoCountAmbiguousWriteError("timed out"))
    master = FakeMasterData(invoices=[matching_invoice()])
    invoice_service = service(tmp_path, master=master, client=client)
    draft = make_draft()

    with pytest.raises(AutoCountAmbiguousWriteError):
        await invoice_service.issue(draft)
    result = await invoice_service.issue(draft)

    assert result.invoice_number == "INV-2026-0001"
    assert len(client.writes) == 1
    assert invoice_service.requests.get(draft.idempotency_key).status is RequestStatus.SUCCEEDED
