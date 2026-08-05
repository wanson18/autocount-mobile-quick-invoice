"""Historical price lookup for the confirmation preview.

Proves ``get_latest_price`` returns the most recent non-cancelled issued
price for a customer/item pair with traceable source invoice metadata, skips
cancelled invoices and unrelated items, returns ``None`` for no history, and
uses a bounded 90-day window. The draft-aware ``get_price_history`` helper
deduplicates identical items into one adapter call each.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import (
    PRICE_HISTORY_WINDOW_DAYS,
    AutoCountMasterDataAdapter,
)
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import (
    InvoiceLineSummary,
    InvoiceSummary,
    PriceHistory,
)
from app.services.price_history import get_price_history

KEY_ID = "key-id-42"
API_KEY = "api-key-secret-abc-123"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"

SDN_BHD = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id=SDN_BHD_AB,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


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
    doc_no,
    doc_date,
    details,
    cancelled=False,
    doc_key=None,
):
    return {
        "master": {
            "docKey": doc_key or doc_no,
            "docNo": doc_no,
            "docDate": doc_date,
            "debtorCode": "C001",
            "total": 100.0,
            "cancelled": cancelled,
        },
        "details": details,
    }


def oil_detail(unit_price):
    return {"productCode": "OIL-5KG", "qty": 2, "unitPrice": unit_price}


def history_rows():
    return [
        invoice_row(
            doc_no="INV-1",
            doc_date="2026-07-20",
            details=[oil_detail(28.0)],
        ),
        invoice_row(
            doc_no="INV-2",
            doc_date="2026-07-25",
            details=[oil_detail(29.0)],
        ),
        invoice_row(
            doc_no="INV-3",
            doc_date="2026-07-30",
            details=[{"productCode": "OTHER", "qty": 1, "unitPrice": 5.0}],
        ),
        invoice_row(
            doc_no="INV-4",
            doc_date="2026-08-01",
            details=[oil_detail(31.5)],
            cancelled=True,
        ),
    ]


def listing_handler(*rows):
    def handler(request):
        return httpx.Response(200, json={"data": list(rows), "totalCount": len(rows)})

    return handler


def test_get_latest_price_returns_most_recent_prior_price_with_source():
    client, adapter = make_adapter(listing_handler(*history_rows()))

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    history = run(client, call)

    assert history == PriceHistory(
        item_id="OIL-5KG",
        customer_id="C001",
        unit_price=Decimal("29.00"),
        source_invoice_number="INV-2",
        source_invoice_date="2026-07-25",
    )


def test_get_latest_price_skips_cancelled_invoices():
    client, adapter = make_adapter(
        listing_handler(
            invoice_row(
                doc_no="INV-1",
                doc_date="2026-07-20",
                details=[oil_detail(28.0)],
            ),
            invoice_row(
                doc_no="INV-2",
                doc_date="2026-08-01",
                details=[oil_detail(31.5)],
                cancelled=True,
            ),
        )
    )

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    history = run(client, call)

    assert history.source_invoice_number == "INV-1"
    assert history.unit_price == Decimal("28.00")


def test_get_latest_price_returns_none_for_no_history():
    client, adapter = make_adapter(listing_handler())

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    assert run(client, call) is None


def test_get_latest_price_returns_none_when_item_never_sold_to_customer():
    client, adapter = make_adapter(
        listing_handler(
            invoice_row(
                doc_no="INV-1",
                doc_date="2026-07-20",
                details=[{"productCode": "OTHER", "qty": 1, "unitPrice": 5.0}],
            )
        )
    )

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    assert run(client, call) is None


def test_get_latest_price_returns_none_when_all_invoices_cancelled():
    client, adapter = make_adapter(
        listing_handler(
            invoice_row(
                doc_no="INV-1",
                doc_date="2026-07-20",
                details=[oil_detail(28.0)],
                cancelled=True,
            )
        )
    )

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    assert run(client, call) is None


def test_get_latest_price_searches_bounded_90_day_window():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    client, adapter = make_adapter(handler)

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    run(client, call)

    body = json.loads(captured[0].content)
    created = body["filter"]["createdDate"]
    assert created["to"] == NOW.isoformat()
    expected_from = (NOW - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)).isoformat()
    assert created["from"] == expected_from


def test_get_latest_price_uses_company_account_book():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    client, adapter = make_adapter(handler)

    def call():
        return adapter.get_latest_price(SDN_BHD, "C001", "OIL-5KG", now=NOW)

    run(client, call)
    assert captured[0].url.path == f"/{SDN_BHD_AB}/invoice/listing"


class FakePriceMaster:
    def __init__(self, history):
        self.history = history
        self.calls = []

    async def get_latest_price(self, company, customer_id, item_id):
        self.calls.append((company, customer_id, item_id))
        return self.history.get(item_id)


def make_draft(*, items=("OIL-5KG", "OIL-5KG", "RICE-10KG")):
    return InvoiceDraftInput(
        company=CompanyKey.SDN_BHD,
        invoice_date=date(2026, 8, 5),
        customer_id="C001",
        delivery_address_id="C001:delivery",
        lines=[
            {
                "item_id": item_id,
                "quantity": Decimal("1"),
                "unit_price": Decimal("10.00"),
                "original_unit_price": Decimal("10.00"),
            }
            for item_id in items
        ],
        idempotency_key="preview-1",
    )


@pytest.mark.asyncio
async def test_get_price_history_deduplicates_identical_items():
    history = {
        "OIL-5KG": PriceHistory(
            "OIL-5KG", "C001", Decimal("29.00"), "INV-2", "2026-07-25"
        )
    }
    master = FakePriceMaster(history)

    result = await get_price_history(master, SDN_BHD, make_draft())

    assert result == {
        "OIL-5KG": history["OIL-5KG"],
        "RICE-10KG": None,
    }
    assert sorted(master.calls) == [
        (SDN_BHD, "C001", "OIL-5KG"),
        (SDN_BHD, "C001", "RICE-10KG"),
    ]


@pytest.mark.asyncio
async def test_get_price_history_maps_missing_history_to_none():
    master = FakePriceMaster({})

    result = await get_price_history(master, SDN_BHD, make_draft(items=("OIL-5KG",)))

    assert result == {"OIL-5KG": None}
