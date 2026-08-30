"""Cloud report links must stay inside the selected AutoCount company."""

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_master_data
from app.main import app
from app.models.master_data import InvoiceLineSummary, InvoiceSummary


class FakeMasterData:
    def __init__(self):
        self.calls = []

    async def get_invoice(self, company, invoice_no):
        self.calls.append((company, invoice_no))
        return InvoiceSummary(
            id="304",
            doc_no=invoice_no,
            doc_date="2026-08-29",
            debtor_code="C001",
            total=Decimal("980.00"),
            lines=(
                InvoiceLineSummary(
                    product_code="ITEM-1",
                    qty=Decimal("20"),
                    unit_price=Decimal("49.00"),
                ),
            ),
        )


@pytest.fixture
def report_client(monkeypatch):
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", "ab-ent")
    monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", "ab-sdn")
    # This was the old single-template configuration. It must not be reused
    # for an Enterprise request.
    monkeypatch.setenv(
        "AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE",
        "https://accounting-report.autocountcloud.com/rpt/63688/invoice?"
        "reportName=WANSON+SDN+BHD+E-INVOICE+&docKey={doc_key}",
    )
    master = FakeMasterData()
    app.dependency_overrides[get_master_data] = lambda: master
    yield TestClient(app), master
    app.dependency_overrides.clear()


def test_enterprise_report_uses_selected_book_and_confirmed_invoice_key(
    report_client,
):
    client, master = report_client

    response = client.get(
        "/api/enterprise/invoices/INV-31190/cloud-report",
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert parsed.path == "/rpt/ab-ent/invoice"
    assert query["reportName"] == ["Wanson Enterprise Invoice"]
    assert query["docKey"] == ["304"]
    assert master.calls[0][0].account_book_id == "ab-ent"
    assert master.calls[0][1] == "INV-31190"
