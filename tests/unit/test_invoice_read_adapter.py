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
