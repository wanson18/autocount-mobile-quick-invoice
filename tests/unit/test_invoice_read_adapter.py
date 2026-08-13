"""Invoice read adapter contract: get_invoice and list_recent_invoices.

``GET /{accountBookId}/invoice?docNo=`` returns the same documented invoice
view model (``master`` + ``details``) as a listing row, so it normalises
through the same path. A mismatched document number fails closed.

``POST /{accountBookId}/invoice/listing`` with the documented ``date``
(docDate) filter and no debtor filter backs the recent-invoice browse; rows
come back newest first so the mobile page renders what it receives.
"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.autocount.errors import AutoCountDataError
from app.config import CompanyConfig
from app.models.company import CompanyKey

KEY_ID = "key-id-42"
API_KEY = "api-key-secret-abc-123"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"

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


def invoice_view(
    *,
    doc_key="9001",
    doc_no="I-000123",
    doc_date="2026-08-13",
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
            {
                "productCode": "ITEM-1",
                "description": "Cooking Oil 5kg",
                "qty": 2,
                "unitPrice": 31.5,
            }
        ],
    }


def listing_payload(*rows):
    return {"data": list(rows), "totalCount": len(rows)}


# ---------------------------------------------------------------------------
# get_invoice
# ---------------------------------------------------------------------------


def test_get_invoice_normalises_the_documented_view_model():
    def handler(request):
        assert request.url.path == f"/{SDN_BHD_AB}/invoice"
        assert request.url.params["docNo"] == "I-000123"
        return httpx.Response(200, json=invoice_view())

    client, adapter = make_adapter(handler)
    invoice = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))

    assert invoice.id == "9001"
    assert invoice.doc_no == "I-000123"
    assert invoice.doc_date == "2026-08-13"
    assert invoice.debtor_code == "C001"
    assert invoice.is_cancelled is False
    assert invoice.total == Decimal("63.0")
    assert len(invoice.lines) == 1
    assert invoice.lines[0].product_code == "ITEM-1"
    assert invoice.lines[0].description == "Cooking Oil 5kg"
    assert invoice.lines[0].qty == Decimal("2")
    assert invoice.lines[0].unit_price == Decimal("31.5")


def test_get_invoice_carries_the_header_fields_an_update_must_echo():
    def handler(request):
        payload = invoice_view()
        payload["master"].update(
            {
                "debtorName": "TANL MARKETING",
                "creditTerm": "C.O.D.",
                "salesLocation": "HQ",
            }
        )
        return httpx.Response(200, json=payload)

    client, adapter = make_adapter(handler)
    inv = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))

    assert inv.debtor_name == "TANL MARKETING"
    assert inv.credit_term == "C.O.D."
    assert inv.sales_location == "HQ"


def test_absent_echo_fields_are_blank_not_an_error():
    # Listing rows feed price history and reconciliation, which never write;
    # a row without these must not break them.
    def handler(request):
        return httpx.Response(200, json=invoice_view())

    client, adapter = make_adapter(handler)
    inv = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))

    assert (inv.debtor_name, inv.credit_term, inv.sales_location) == ("", "", "")


def test_a_malformed_echo_field_fails_closed():
    def handler(request):
        payload = invoice_view()
        payload["master"]["creditTerm"] = {"unexpected": "object"}
        return httpx.Response(200, json=payload)

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_routes_to_the_selected_account_book():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=invoice_view())

    client, adapter = make_adapter(handler)
    run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))

    assert captured[0].url.path == f"/{SDN_BHD_AB}/invoice"


def test_get_invoice_rejects_a_different_document_number():
    def handler(request):
        return httpx.Response(200, json=invoice_view(doc_no="I-999999"))

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError) as exc:
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))
    # The other invoice's identity must not leak into the error text.
    assert "I-999999" not in str(exc.value)


def test_get_invoice_missing_description_is_blank_not_an_error():
    def handler(request):
        return httpx.Response(
            200,
            json=invoice_view(
                details=[{"productCode": "ITEM-1", "qty": 1, "unitPrice": 10}]
            ),
        )

    client, adapter = make_adapter(handler)
    invoice = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))
    assert invoice.lines[0].description == ""


def test_get_invoice_rejects_a_malformed_description():
    def handler(request):
        return httpx.Response(
            200,
            json=invoice_view(
                details=[
                    {
                        "productCode": "ITEM-1",
                        "description": {"unexpected": "object"},
                        "qty": 1,
                        "unitPrice": 10,
                    }
                ]
            ),
        )

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_rejects_a_non_object_payload():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_rejects_a_payload_without_master():
    def handler(request):
        return httpx.Response(200, json={"details": []})

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_rejects_a_blank_document_number():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made")

    client, adapter = make_adapter(handler)
    with pytest.raises(ValueError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "  "))


def test_get_invoice_preserves_a_cancelled_flag():
    def handler(request):
        return httpx.Response(200, json=invoice_view(cancelled=True))

    client, adapter = make_adapter(handler)
    invoice = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))
    assert invoice.is_cancelled is True


# ---------------------------------------------------------------------------
# list_recent_invoices
# ---------------------------------------------------------------------------


def recent(adapter, date_from="2026-07-14", date_to="2026-08-13"):
    return lambda: adapter.list_recent_invoices(
        SDN_BHD, date_from=date_from, date_to=date_to
    )


def test_list_recent_invoices_filters_on_document_date_without_a_debtor():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=listing_payload(invoice_view()))

    client, adapter = make_adapter(handler)
    run(client, recent(adapter))

    body = json.loads(captured[0].content)
    assert captured[0].url.path == f"/{SDN_BHD_AB}/invoice/listing"
    assert body["page"] == 1
    assert body["filter"] == {"date": {"from": "2026-07-14", "to": "2026-08-13"}}
    assert "debtorCode" not in body["filter"]
    assert "createdDate" not in body["filter"]


def test_list_recent_invoices_returns_newest_first():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", doc_date="2026-08-01"),
        invoice_view(doc_key="3", doc_no="I-000003", doc_date="2026-08-13"),
        invoice_view(doc_key="2", doc_no="I-000002", doc_date="2026-08-07"),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(client, recent(adapter))

    assert [i.doc_no for i in invoices] == ["I-000003", "I-000002", "I-000001"]


def test_list_recent_invoices_breaks_same_day_ties_on_document_number():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", doc_date="2026-08-13"),
        invoice_view(doc_key="2", doc_no="I-000002", doc_date="2026-08-13"),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(client, recent(adapter))

    assert [i.doc_no for i in invoices] == ["I-000002", "I-000001"]


def test_list_recent_invoices_includes_cancelled_invoices_flagged():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", cancelled=True),
        invoice_view(doc_key="2", doc_no="I-000002", cancelled=False),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(client, recent(adapter))

    assert {i.doc_no: i.is_cancelled for i in invoices} == {
        "I-000001": True,
        "I-000002": False,
    }


def test_list_recent_invoices_returns_empty_when_the_book_has_none():
    def handler(request):
        return httpx.Response(200, json=listing_payload())

    client, adapter = make_adapter(handler)
    assert run(client, recent(adapter)) == []


def test_list_recent_invoices_pages_until_the_total_is_consumed():
    pages = {
        1: {"data": [invoice_view(doc_key="1", doc_no="I-000001")], "totalCount": 2},
        2: {"data": [invoice_view(doc_key="2", doc_no="I-000002")], "totalCount": 2},
    }

    def handler(request):
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json=pages[page])

    client, adapter = make_adapter(handler)
    invoices = run(client, recent(adapter))

    assert sorted(i.doc_no for i in invoices) == ["I-000001", "I-000002"]


def test_list_recent_invoices_rejects_an_inconsistent_total():
    pages = {
        1: {"data": [invoice_view(doc_key="1", doc_no="I-000001")], "totalCount": 2},
        2: {"data": [invoice_view(doc_key="2", doc_no="I-000002")], "totalCount": 9},
    }

    def handler(request):
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json=pages[page])

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, recent(adapter))


def test_list_recent_invoices_rejects_a_blank_date_bound():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made")

    client, adapter = make_adapter(handler)
    with pytest.raises(ValueError):
        run(client, recent(adapter, date_from="  "))
