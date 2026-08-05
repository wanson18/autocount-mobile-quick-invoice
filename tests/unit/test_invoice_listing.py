"""Task 8 - invoice listing adapter contract.

Proves ``search_invoices`` against the documented Cloud Accounting Integration
API contract: ``POST /{accountBookId}/invoice/listing`` whose body carries the
``debtorCode`` multiSelect and ``createdDate`` from/to filters, rows are the
documented invoice view model (``master`` + ``details``), and every invoice is
normalised to ``InvoiceSummary`` with the ``docKey`` identity and exact
``Decimal`` totals, quantities, and unit prices. Malformed rows fail closed
and never leak response bodies.
"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.autocount.errors import (
    AutoCountDataError,
    AutoCountRejectedError,
    AutoCountTransportError,
)
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.master_data import InvoiceLineSummary, InvoiceSummary

KEY_ID = "key-id-42"
API_KEY = "api-key-secret-abc-123"
ENTERPRISE_AB = "ab-wanson-enterprise-001"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"

ENTERPRISE = CompanyConfig(
    key=CompanyKey.ENTERPRISE,
    name="Wanson Enterprise",
    account_book_id=ENTERPRISE_AB,
)
SDN_BHD = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id=SDN_BHD_AB,
)


def make_adapter(handler):
    transport = httpx.MockTransport(handler)
    client = AutoCountClient(KEY_ID, API_KEY, transport=transport)
    return client, AutoCountMasterDataAdapter(client)


def run(client, coro_fn):
    async def _run():
        try:
            return await coro_fn()
        finally:
            await client.aclose()

    return asyncio.run(_run())


def invoice_row(
    *,
    doc_key="9001",
    doc_no="INV-2026-0001",
    doc_date="2026-08-04",
    debtor_code="C001",
    total=63.0,
    details=None,
    cancelled=False,
):
    return {
        "master": {
            "docKey": doc_key,
            "docNo": doc_no,
            "docDate": doc_date,
            "debtorCode": debtor_code,
            "total": total,
            "cancelled": cancelled,
        },
        "details": details if details is not None else [
            {"productCode": "ITEM-1", "qty": 2, "unitPrice": 31.5}
        ],
    }


def listing_payload(*rows):
    return {"data": list(rows), "totalCount": len(rows)}


def test_search_invoices_routes_each_company_to_its_own_account_book():
    captured = []

    def handler(request):
        captured.append(request)
        if request.url.path == f"/{ENTERPRISE_AB}/invoice/listing":
            return httpx.Response(200, json=listing_payload(invoice_row(doc_no="E-1")))
        if request.url.path == f"/{SDN_BHD_AB}/invoice/listing":
            return httpx.Response(200, json=listing_payload(invoice_row(doc_no="S-1")))
        return httpx.Response(500, json={})

    client, adapter = make_adapter(handler)

    async def lookups():
        results = []
        for company, customer_id in ((ENTERPRISE, "E-CUST"), (SDN_BHD, "S-CUST")):
            invoices = await adapter.search_invoices(
                company,
                customer_id=customer_id,
                date_from="2026-08-04T00:00:00+00:00",
                date_to="2026-08-04T02:00:00+00:00",
            )
            results.append(invoices)
        return results

    results = run(client, lookups)

    assert [r[0].doc_no for r in results] == ["E-1", "S-1"]
    assert [r.url.path for r in captured] == [
        f"/{ENTERPRISE_AB}/invoice/listing",
        f"/{SDN_BHD_AB}/invoice/listing",
    ]


def test_search_invoices_sends_documented_filter_body():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=listing_payload(invoice_row()))

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD,
            customer_id="C001",
            date_from="2026-08-04T10:00:00+00:00",
            date_to="2026-08-04T12:00:00+00:00",
        )

    run(client, call)

    assert captured[0].method == "POST"
    body = json.loads(captured[0].content)
    assert body == {
        "page": 1,
        "filter": {
            "debtorCode": {"multiSelect": ["C001"]},
            "createdDate": {"from": "2026-08-04T10:00:00+00:00", "to": "2026-08-04T12:00:00+00:00"},
        },
    }


def test_search_invoices_normalises_rows_to_exact_summaries():
    def handler(request):
        return httpx.Response(
            200,
            json=listing_payload(
                invoice_row(
                    doc_key=9001,
                    doc_no="INV-2026-0001",
                    doc_date="2026-08-04",
                    debtor_code="C001",
                    total="63.00",
                    details=[
                        {"productCode": "ITEM-1", "qty": "2", "unitPrice": "31.50"},
                        {"productCode": "ITEM-2", "qty": 1, "unitPrice": 0.0},
                    ],
                )
            ),
        )

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="2026-08-04T00:00:00+00:00", date_to="2026-08-04T01:00:00+00:00"
        )

    invoices = run(client, call)

    assert invoices == [
        InvoiceSummary(
            id="9001",
            doc_no="INV-2026-0001",
            doc_date="2026-08-04",
            debtor_code="C001",
            total=Decimal("63.00"),
            lines=(
                InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50")),
                InvoiceLineSummary("ITEM-2", Decimal("1"), Decimal("0")),
            ),
            is_cancelled=False,
        )
    ]


def test_search_invoices_marks_cancelled_invoice():
    def handler(request):
        return httpx.Response(
            200,
            json=listing_payload(invoice_row(cancelled=True)),
        )

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    invoices = run(client, call)
    assert invoices[0].is_cancelled is True


def test_search_invoices_consumes_all_pages():
    def handler(request):
        page = json.loads(request.content)["page"]
        if page == 1:
            rows = [invoice_row(doc_key="9001", doc_no="INV-1")]
            return httpx.Response(200, json={"data": rows, "totalCount": 2})
        rows = [invoice_row(doc_key="9002", doc_no="INV-2")]
        return httpx.Response(200, json={"data": rows, "totalCount": 2})

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    invoices = run(client, call)
    assert [i.doc_no for i in invoices] == ["INV-1", "INV-2"]


@pytest.mark.parametrize(
    "row",
    [
        None,
        "not-a-dict",
        {},
        {"master": None, "details": []},
        {"master": {"docKey": 9001}, "details": []},
        {"master": {"docKey": "  ", "docNo": "X"}, "details": []},
        {"master": {"docKey": 9001, "docNo": "X", "docDate": "2026-08-04", "debtorCode": "C001"}, "details": None},
        {"master": {"docKey": 9001, "docNo": "X", "docDate": "2026-08-04", "debtorCode": "C001"}, "details": "oops"},
    ],
)
def test_malformed_invoice_rows_fail_closed(row):
    def handler(request):
        return httpx.Response(200, json={"data": [row], "totalCount": 1})

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    with pytest.raises(AutoCountDataError):
        run(client, call)


@pytest.mark.parametrize(
    "detail",
    [
        None,
        "not-a-dict",
        {},
        {"productCode": "ITEM-1", "qty": 0, "unitPrice": 1},
        {"productCode": "ITEM-1", "qty": -2, "unitPrice": 1},
        {"productCode": "ITEM-1", "qty": 2, "unitPrice": -1},
        {"productCode": "ITEM-1", "qty": "abc", "unitPrice": 1},
        {"productCode": "ITEM-1", "qty": 2, "unitPrice": "NaN"},
        {"productCode": "", "qty": 2, "unitPrice": 1},
        {"productCode": "ITEM-1", "qty": 2},
        {"productCode": "ITEM-1", "unitPrice": 1},
        {"qty": 2, "unitPrice": 1},
    ],
)
def test_malformed_invoice_details_fail_closed(detail):
    def handler(request):
        return httpx.Response(
            200,
            json=listing_payload(invoice_row(details=[detail])),
        )

    client, adapter = make_adapter(handler)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    with pytest.raises(AutoCountDataError):
        run(client, call)


def test_malformed_total_or_cancelled_flag_fail_closed():
    for master in (
        {"docKey": 9001, "docNo": "X", "docDate": "2026-08-04", "debtorCode": "C001", "total": "NaN"},
        {"docKey": 9001, "docNo": "X", "docDate": "2026-08-04", "debtorCode": "C001", "total": -1},
        {"docKey": 9001, "docNo": "X", "docDate": "2026-08-04", "debtorCode": "C001", "total": 1, "cancelled": "yes"},
    ):
        def handler(request, master=master):
            return httpx.Response(
                200,
                json={
                    "data": [{"master": master, "details": []}],
                    "totalCount": 1,
                },
            )

        client, adapter = make_adapter(handler)

        def call():
            return adapter.search_invoices(
                SDN_BHD, customer_id="C001", date_from="d", date_to="e"
            )

        with pytest.raises(AutoCountDataError):
            run(client, call)


def test_search_invoices_propagates_rejection_and_transport_errors():
    def rejected(request):
        return httpx.Response(400, json={"message": "bad filter"})

    client, adapter = make_adapter(rejected)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    with pytest.raises(AutoCountRejectedError, match="bad filter"):
        run(client, call)

    def transport_error(request):
        raise httpx.ConnectError("boom")

    client, adapter = make_adapter(transport_error)

    def call():
        return adapter.search_invoices(
            SDN_BHD, customer_id="C001", date_from="d", date_to="e"
        )

    with pytest.raises(AutoCountTransportError):
        run(client, call)
