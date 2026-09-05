"""Task 11 - REST API over the invoice service.

Exercises the FastAPI app with ``TestClient`` and dependency overrides backed
by the real ``InvoiceService`` (with fake master data and a mock AutoCount
transport), the real company resolver, and a temporary idempotency database.
Covers: company listing without account-book IDs, master-data search,
addresses, issue + idempotent replay, structured validation errors with field
paths, price preview, fail-closed PDF, sanitised upstream errors, and the
Custom GPT Action OpenAPI schema.
"""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.autocount import AutoCountClient
from app.autocount.errors import AutoCountUnsupportedError
from app.config import get_company
from app.dependencies import get_invoice_service, get_master_data
from app.main import app
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceSummary,
    PriceHistory,
    ProductSummary,
)
from app.repositories.request_repository import RequestRepository
from app.services.invoice_service import EInvoiceStatus, InvoiceService

KEY_ID = "api-key-id-99"
API_KEY = "api-key-secret-xyz-789"
ENTERPRISE_AB = "ab-wanson-enterprise-001"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"


class FakeMasterData:
    def __init__(self):
        self.customer = CustomerSummary(
            "C001", "C001", "Customer One", tax_entity="TIN:C23453889090"
        )
        self.product = ProductSummary(
            "ITEM-1",
            "ITEM-1",
            "Cooking Oil",
            Decimal("30.00"),
            classification_code="022",
        )
        self.address = DeliveryAddress(
            "C001:delivery", "Default delivery address", "1 Main Street"
        )
        self.history = {
            "ITEM-1": PriceHistory(
                "ITEM-1", "C001", Decimal("29.00"), "INV-2", "2026-07-25"
            )
        }
        self.lookup_calls = []

    async def search_customers(self, company, query):
        self.lookup_calls.append(("customers", company))
        return [self.customer] if "C001" in query.upper() else []

    async def get_customer(self, company, customer_id):
        self.lookup_calls.append(("customer", company, customer_id))
        return self.customer

    async def get_delivery_addresses(self, company, customer_id):
        self.lookup_calls.append(("addresses", company, customer_id))
        return [self.address] if customer_id == "C001" else []

    async def search_items(self, company, query):
        self.lookup_calls.append(("items", company))
        return [self.product] if "oil" in query.lower() else []

    async def get_item(self, company, item_id):
        self.lookup_calls.append(("item", company, item_id))
        if item_id != self.product.id:
            raise AssertionError(f"unexpected item lookup: {item_id}")
        return self.product

    async def search_invoices(self, company, *, customer_id, date_from, date_to):
        return []

    async def get_latest_price(self, company, customer_id, item_id):
        self.lookup_calls.append(("price", company, customer_id, item_id))
        return self.history.get(item_id)

    async def get_invoice_pdf(self, company, invoice_id):
        raise AutoCountUnsupportedError(
            "AutoCount provides no documented invoice PDF endpoint"
        )

    async def get_invoice(self, company, invoice_no):
        return InvoiceSummary(
            id="inv-1",
            doc_no=invoice_no,
            doc_date="2026-08-05",
            debtor_code="C001",
            total=Decimal("63.00"),
            lines=(),
        )


def make_issue_service(tmp_path, *, client=None):
    return InvoiceService(
        company_resolver=get_company,
        master_data=FakeMasterData(),
        client=client or _default_client(),
        requests=RequestRepository(tmp_path / "requests.db"),
    )


def _default_client():
    """Mirrors AutoCount's real Create Invoice contract: POST /invoice
    returns a bare 201 with only a ``location`` header (no JSON body), and
    GET /invoice (used by InvoiceService to resolve the created invoice's
    identity) returns the documented view model."""

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/invoice"):
            location = str(request.url.copy_with(query=b"docNo=INV-2026-0001"))
            return httpx.Response(201, headers={"location": location})
        if request.method == "GET" and request.url.path.endswith("/invoice"):
            return httpx.Response(
                200,
                json={"master": {"docKey": "inv-1", "docNo": "INV-2026-0001"}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return AutoCountClient(KEY_ID, API_KEY, transport=httpx.MockTransport(handler))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", ENTERPRISE_AB)
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", SDN_BHD_AB)
    monkeypatch.setenv("AUTOCOUNT_API_KEY_ID", KEY_ID)
    monkeypatch.setenv("AUTOCOUNT_API_KEY", API_KEY)
    master = FakeMasterData()
    service = make_issue_service(tmp_path)
    app.dependency_overrides[get_master_data] = lambda: master
    app.dependency_overrides[get_invoice_service] = lambda: service
    yield TestClient(app), master, service
    app.dependency_overrides.clear()


VALID_DRAFT = {
    "company": "sdn_bhd",
    "invoice_date": "2026-08-05",
    "customer_id": "C001",
    "delivery_address_id": "C001:delivery",
    "lines": [
        {
            "item_id": "ITEM-1",
            "quantity": "2",
            "unit_price": "31.50",
            "original_unit_price": "30.00",
        }
    ],
    "idempotency_key": "api-issue-1",
}


def test_get_companies_returns_two_listings_without_account_book_ids(api):
    client, _, _ = api
    response = client.get("/api/companies")
    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {"key": "enterprise", "name": "Wanson Enterprise"},
            {"key": "sdn_bhd", "name": "Wanson Enterprise (M) Sdn Bhd"},
        ]
    }
    body = response.text
    assert ENTERPRISE_AB not in body and SDN_BHD_AB not in body


def test_search_customers_returns_matching_customers(api):
    client, master, _ = api
    response = client.get("/api/sdn_bhd/customers", params={"q": "C001"})
    assert response.status_code == 200
    assert response.json() == {
        "data": [{"id": "C001", "code": "C001", "name": "Customer One"}]
    }
    assert master.lookup_calls[-1][0] == "customers"


def test_search_customers_unknown_company_is_422_with_field_path(api):
    client, _, _ = api
    response = client.get("/api/nonsense/customers")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(err["loc"] == ["path", "company"] for err in detail)


def test_get_customer_addresses_returns_owned_addresses(api):
    client, _, _ = api
    response = client.get("/api/enterprise/customers/C001/addresses")
    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "id": "C001:delivery",
                "label": "Default delivery address",
                "address_text": "1 Main Street",
            }
        ]
    }


def test_search_products_returns_stringified_prices(api):
    client, _, _ = api
    response = client.get("/api/enterprise/products", params={"q": "oil"})
    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "id": "ITEM-1",
                "code": "ITEM-1",
                "name": "Cooking Oil",
                "default_price": "30.00",
            }
        ]
    }


def test_issue_invoice_returns_created_result(api):
    client, _, _ = api
    response = client.post("/api/invoices", json=VALID_DRAFT)
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["invoice_id"] == "inv-1"
    assert body["invoice_number"] == "INV-2026-0001"
    assert body["price_overrides"] == [
        {
            "item_id": "ITEM-1",
            "original_unit_price": "30.00",
            "issued_unit_price": "31.50",
        }
    ]
    assert body["einvoice"] == {
        "status": EInvoiceStatus.NOT_REQUESTED.value,
        "error_message": None,
    }


def test_mobile_issue_request_enables_native_einvoice():
    script = (Path(__file__).parents[2] / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "submit_einvoice: true" in script
    assert "submit_einvoice: false" not in script


def test_issue_invoice_same_key_replays_without_second_create(api):
    client, _, _ = api
    first = client.post("/api/invoices", json=VALID_DRAFT)
    second = client.post("/api/invoices", json=VALID_DRAFT)
    assert first.status_code == second.status_code == 201
    assert (
        first.json()["data"]["invoice_number"]
        == second.json()["data"]["invoice_number"]
    )


def test_issue_invoice_same_key_different_payload_is_conflict(api):
    client, _, _ = api
    assert client.post("/api/invoices", json=VALID_DRAFT).status_code == 201
    changed = dict(VALID_DRAFT)
    changed["lines"][0]["quantity"] = "3"
    response = client.post("/api/invoices", json=changed)
    assert response.status_code == 409
    assert response.json()["error"] == "idempotency_conflict"


def test_issue_invoice_validation_errors_carry_field_paths(api):
    client, _, _ = api
    response = client.post(
        "/api/invoices", json={"company": "sdn_bhd"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    locs = [tuple(err["loc"]) for err in detail]
    assert ("body", "customer_id") in locs
    assert ("body", "lines") in locs
    assert ("body", "idempotency_key") in locs


def test_issue_invoice_empty_lines_are_rejected(api):
    client, _, _ = api
    draft = dict(VALID_DRAFT)
    draft["lines"] = []
    response = client.post("/api/invoices", json=draft)
    assert response.status_code == 422
    assert any(
        err["loc"] == ["body", "lines"] for err in response.json()["detail"]
    )


def test_issue_invoice_unknown_company_in_body_is_422(api):
    client, _, _ = api
    draft = dict(VALID_DRAFT)
    draft["company"] = "other"
    response = client.post("/api/invoices", json=draft)
    assert response.status_code == 422
    assert any(
        err["loc"] == ["body", "company"] for err in response.json()["detail"]
    )


def test_issue_invoice_rejects_client_supplied_account_book_id(api):
    client, _, _ = api
    draft = dict(VALID_DRAFT)
    draft["account_book_id"] = SDN_BHD_AB
    response = client.post("/api/invoices", json=draft)
    assert response.status_code == 422
    assert any(
        err["loc"] == ["body", "account_book_id"]
        for err in response.json()["detail"]
    )


def test_issue_invoice_customer_not_in_company_is_400(api):
    client, _, _ = api
    draft = dict(VALID_DRAFT)
    draft["customer_id"] = "UNKNOWN"
    draft["delivery_address_id"] = "UNKNOWN:delivery"
    response = client.post("/api/invoices", json=draft)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_invoice"
    assert "customer" in response.json()["message"]


def test_issue_invoice_autocount_rejection_is_502_and_sanitised(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", ENTERPRISE_AB)
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", SDN_BHD_AB)

    def handler(request):
        return httpx.Response(400, json={"message": f"bad debtor {API_KEY}"})

    rejecting = AutoCountClient(
        KEY_ID, API_KEY, transport=httpx.MockTransport(handler)
    )
    master = FakeMasterData()
    service = make_issue_service(tmp_path, client=rejecting)
    app.dependency_overrides[get_master_data] = lambda: master
    app.dependency_overrides[get_invoice_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.post("/api/invoices", json=VALID_DRAFT)
        assert response.status_code == 502
        assert response.json()["error"] == "autocount_rejected"
        assert API_KEY not in response.text
        assert SDN_BHD_AB not in response.text
    finally:
        app.dependency_overrides.clear()


def test_issue_invoice_ambiguous_write_is_502_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", ENTERPRISE_AB)
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", SDN_BHD_AB)

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    timing_out = AutoCountClient(
        KEY_ID, API_KEY, transport=httpx.MockTransport(handler)
    )
    master = FakeMasterData()
    service = make_issue_service(tmp_path, client=timing_out)
    app.dependency_overrides[get_master_data] = lambda: master
    app.dependency_overrides[get_invoice_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.post("/api/invoices", json=VALID_DRAFT)
        assert response.status_code == 502
        assert response.json()["error"] == "autocount_ambiguous_write"
        assert response.json()["retryable"] is True
    finally:
        app.dependency_overrides.clear()


def test_get_invoice_pdf_fails_closed_as_501(api):
    client, _, _ = api
    response = client.get("/api/sdn_bhd/invoices/9001/pdf")
    assert response.status_code == 501
    assert response.json()["error"] == "unsupported"
    assert "no documented" in response.json()["message"]


def test_preview_returns_price_history_with_sources(api):
    client, _, _ = api
    response = client.post(
        "/api/invoices/preview",
        json={"company": "sdn_bhd", "customer_id": "C001", "item_ids": ["ITEM-1", "MISSING"]},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {
        "customer_id": "C001",
        "items": [
            {
                "item_id": "ITEM-1",
                "latest_unit_price": "29.00",
                "source_invoice_number": "INV-2",
                "source_invoice_date": "2026-07-25",
            },
            {
                "item_id": "MISSING",
                "latest_unit_price": None,
                "source_invoice_number": None,
                "source_invoice_date": None,
            },
        ],
    }


def test_preview_empty_item_ids_is_422_with_field_path(api):
    client, _, _ = api
    response = client.post(
        "/api/invoices/preview",
        json={"company": "sdn_bhd", "customer_id": "C001", "item_ids": []},
    )
    assert response.status_code == 422
    assert any(
        err["loc"] == ["body", "item_ids"] for err in response.json()["detail"]
    )


def test_missing_company_config_is_clear_500_not_internal_error(api, monkeypatch):
    for var in (
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE",
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD",
        "AUTOCOUNT_API_KEY_ID",
        "AUTOCOUNT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    client, _, _ = api
    response = client.get("/api/companies")
    assert response.status_code == 500
    assert response.json()["error"] == "server_configuration_error"
    # Companies load in CompanyKey order, so enterprise is the first failure
    # reported; the message must name the missing env var, not hide it.
    assert "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE" in response.json()["message"]


def test_openapi_schema_has_operation_ids_and_consequential_flag(api):
    client, _, _ = api
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "AutoCount Mobile Quick Invoice"
    assert "paths" in schema
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method in {"get", "post"}:
                assert operation.get("operationId"), f"{method} {path} lacks operationId"
    issue = schema["paths"]["/api/invoices"]["post"]
    assert issue["x-openai-isConsequential"] is True
    preview = schema["paths"]["/api/invoices/preview"]["post"]
    assert "x-openai-isConsequential" not in preview


def test_openapi_schema_never_exposes_account_book_ids(api):
    client, _, _ = api
    schema_text = client.get("/openapi.json").text
    assert ENTERPRISE_AB not in schema_text
    assert SDN_BHD_AB not in schema_text
    assert KEY_ID not in schema_text
    assert API_KEY not in schema_text


# ---------------------------------------------------------------------------
# static page caching
# ---------------------------------------------------------------------------
#
# Starlette sends ETag and Last-Modified but no Cache-Control, and a response
# with neither Cache-Control nor Expires falls under the browser's heuristic
# freshness rule (RFC 9111 4.2.2). That served a stale app.js after a deploy,
# which is indistinguishable from the deploy not having shipped.


def test_the_page_and_its_script_must_be_revalidated(api):
    client, _, _ = api
    for path in ("/", "/app.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path


def test_revalidating_an_unchanged_script_costs_no_body(api):
    # no-cache means "revalidate", not "re-download": the ETag turns a
    # reload into a 304, which is what keeps the header cheap.
    client, _, _ = api
    etag = client.get("/app.js").headers["etag"]

    revalidated = client.get("/app.js", headers={"If-None-Match": etag})

    assert revalidated.status_code == 304
    assert not revalidated.content


def test_api_responses_are_not_given_cache_headers(api):
    # The static override must not leak onto JSON responses.
    client, _, _ = api
    assert "cache-control" not in client.get("/api/companies").headers


def test_cloud_report_redirect_uses_selected_company_book_and_invoice_key(api):
    client, _, _ = api
    response = client.get(
        "/api/enterprise/invoices/INV-2026-0001/cloud-report",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://accounting-report.autocountcloud.com/rpt/"
        "ab-wanson-enterprise-001/invoice?reportName=Wanson+Enterprise+Invoice"
        "&docKey=inv-1"
    )
    assert SDN_BHD_AB not in response.headers["location"]


def test_cloud_report_redirect_uses_the_sdn_bhd_book_and_report_identity(api):
    # The Sdn Bhd report name keeps its trailing space: that is the configured
    # AutoCount report identity, and the redirect must carry it exactly.
    client, _, _ = api
    response = client.get(
        "/api/sdn_bhd/invoices/INV-2026-0001/cloud-report",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://accounting-report.autocountcloud.com/rpt/"
        "ab-wanson-sdn-bhd-001/invoice?reportName=WANSON+SDN+BHD+E-INVOICE+"
        "&docKey=inv-1"
    )
