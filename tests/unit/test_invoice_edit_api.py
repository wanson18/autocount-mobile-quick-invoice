"""View endpoints for issued invoices, plus the editability rule.

These endpoints are deliberately absent from ``/openapi.json``: the Custom GPT
Action reads that schema, and browsing or editing a live invoice is a
mobile-page workflow. Money always serialises as an exact decimal string.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.autocount.errors import AutoCountRejectedError
from app.dependencies import get_master_data
from app.main import app
from app.models.master_data import InvoiceLineSummary, InvoiceSummary
from app.services.invoice_edit_service import EDIT_WINDOW_DAYS, is_editable

TODAY = date(2026, 8, 13)


def invoice(
    *,
    doc_key="9001",
    doc_no="I-000123",
    doc_date="2026-08-13",
    cancelled=False,
    lines=None,
):
    return InvoiceSummary(
        id=doc_key,
        doc_no=doc_no,
        doc_date=doc_date,
        debtor_code="C001",
        total=Decimal("63.00"),
        lines=tuple(
            lines
            if lines is not None
            else [
                InvoiceLineSummary(
                    product_code="ITEM-1",
                    qty=Decimal("2"),
                    unit_price=Decimal("31.50"),
                    description="Cooking Oil 5kg",
                )
            ]
        ),
        is_cancelled=cancelled,
    )


class FakeMasterData:
    def __init__(self, invoices=None):
        self.invoices = invoices if invoices is not None else [invoice()]
        self.list_calls = []

    async def list_recent_invoices(self, company, *, date_from, date_to):
        self.list_calls.append((company.key.value, date_from, date_to))
        return list(self.invoices)

    async def get_invoice(self, company, invoice_no):
        for inv in self.invoices:
            if inv.doc_no == invoice_no:
                return inv
        raise AutoCountRejectedError(404, "invoice not found")


@pytest.fixture
def client_with(monkeypatch):
    def _build(master):
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", "ab-ent")
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", "ab-sdn")
        app.dependency_overrides[get_master_data] = lambda: master
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# editability rule
# ---------------------------------------------------------------------------


def test_a_recent_uncancelled_invoice_is_editable():
    assert is_editable(invoice(doc_date="2026-08-13"), today=TODAY) is True


def test_a_cancelled_invoice_is_never_editable():
    assert is_editable(invoice(cancelled=True), today=TODAY) is False


def test_an_invoice_on_the_window_boundary_is_still_editable():
    assert is_editable(invoice(doc_date="2026-07-14"), today=TODAY) is True


def test_an_invoice_one_day_past_the_window_is_not_editable():
    assert is_editable(invoice(doc_date="2026-07-13"), today=TODAY) is False


def test_a_future_dated_invoice_is_not_editable():
    assert is_editable(invoice(doc_date="2026-09-01"), today=TODAY) is False


def test_an_unparseable_document_date_fails_closed():
    assert is_editable(invoice(doc_date="not-a-date"), today=TODAY) is False


def test_the_edit_window_is_thirty_days():
    assert EDIT_WINDOW_DAYS == 30


# ---------------------------------------------------------------------------
# list endpoint
# ---------------------------------------------------------------------------


def test_list_invoices_returns_exact_string_money(client_with):
    client = client_with(FakeMasterData())
    response = client.get("/api/sdn_bhd/invoices")

    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["doc_no"] == "I-000123"
    assert row["total"] == "63.00"
    assert isinstance(row["total"], str)
    assert row["line_count"] == 1
    assert row["is_cancelled"] is False


def test_list_invoices_defaults_to_the_edit_window(client_with):
    master = FakeMasterData()
    client = client_with(master)
    client.get("/api/sdn_bhd/invoices")

    _, date_from, date_to = master.list_calls[0]
    assert (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days == (
        EDIT_WINDOW_DAYS
    )


def test_list_invoices_honours_a_narrower_window(client_with):
    master = FakeMasterData()
    client = client_with(master)
    client.get("/api/sdn_bhd/invoices?days=7")

    _, date_from, date_to = master.list_calls[0]
    assert (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days == 7


def test_list_invoices_rejects_a_window_outside_the_allowed_range(client_with):
    client = client_with(FakeMasterData())
    assert client.get("/api/sdn_bhd/invoices?days=31").status_code == 422
    assert client.get("/api/sdn_bhd/invoices?days=0").status_code == 422


def test_list_invoices_routes_to_the_requested_company(client_with):
    master = FakeMasterData()
    client = client_with(master)
    client.get("/api/enterprise/invoices")

    assert master.list_calls[0][0] == "enterprise"


def test_list_invoices_rejects_an_unknown_company(client_with):
    client = client_with(FakeMasterData())
    assert client.get("/api/nope/invoices").status_code == 422


# ---------------------------------------------------------------------------
# detail endpoint
# ---------------------------------------------------------------------------


def test_get_invoice_returns_lines_with_exact_money(client_with):
    client = client_with(FakeMasterData())
    response = client.get("/api/sdn_bhd/invoices/I-000123")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_editable"] is True
    assert data["lines"] == [
        {
            "product_code": "ITEM-1",
            "description": "Cooking Oil 5kg",
            "quantity": "2",
            "unit_price": "31.50",
        }
    ]


def test_get_invoice_marks_a_cancelled_invoice_not_editable(client_with):
    client = client_with(FakeMasterData([invoice(cancelled=True)]))
    response = client.get("/api/sdn_bhd/invoices/I-000123")
    assert response.json()["data"]["is_editable"] is False
    assert response.json()["data"]["is_cancelled"] is True


def test_get_invoice_marks_an_old_invoice_not_editable(client_with):
    client = client_with(FakeMasterData([invoice(doc_date="2020-01-01")]))
    response = client.get("/api/sdn_bhd/invoices/I-000123")
    assert response.json()["data"]["is_editable"] is False


def test_get_unknown_invoice_is_a_clean_404(client_with):
    client = client_with(FakeMasterData([]))
    response = client.get("/api/sdn_bhd/invoices/I-999999")

    assert response.status_code == 404
    assert response.json()["error"] == "invoice_not_found"


def test_an_upstream_failure_other_than_404_stays_a_502(client_with):
    class Broken(FakeMasterData):
        async def get_invoice(self, company, invoice_no):
            raise AutoCountRejectedError(500, "upstream exploded")

    client = client_with(Broken())
    response = client.get("/api/sdn_bhd/invoices/I-000123")

    assert response.status_code == 502
    assert response.json()["error"] == "autocount_rejected"


# ---------------------------------------------------------------------------
# GPT Action isolation
# ---------------------------------------------------------------------------


def test_view_endpoints_are_absent_from_the_gpt_schema(client_with):
    client = client_with(FakeMasterData())
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/{company}/invoices" not in paths
    assert "/api/{company}/invoices/{doc_no}" not in paths


def test_the_create_endpoint_the_gpt_uses_is_still_published(client_with):
    client = client_with(FakeMasterData())
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/invoices" in paths
    assert "/api/invoices/preview" in paths
