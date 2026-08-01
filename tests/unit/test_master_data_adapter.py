"""Task 4 — Read-only AutoCount master-data adapter.

Behaviour proven through ``httpx.MockTransport`` (no real network) against the
documented Cloud Accounting Integration API contracts:

- customer listing is ``GET /debtor/listing`` with ``page`` and ``activeOnly=true``;
- debtor lookup is ``GET /debtor?code=...`` with one ``deliverAddress`` /
  ``deliverPostCode`` on the debtor object;
- product listing is a read-semantic ``POST /product/listing`` whose body asks
  for active products only;
- product lookup is ``GET /product?code=...`` wrapping master data under ``product``.

Assertions cover: company isolation, page consumption, local case-insensitive
code/name filtering, exact ``Decimal`` prices, stable default delivery-address
construction, code-identity verification, and fail-closed behaviour for empty
IDs, malformed envelopes/rows/totals, inconsistent pagination, and unsafe
prices. Transport/rejection errors propagate unchanged.
"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import _LISTING_PAGE_LIMIT, AutoCountMasterDataAdapter
from app.autocount.errors import (
    AutoCountDataError,
    AutoCountRejectedError,
    AutoCountTransportError,
)
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.master_data import CustomerSummary, DeliveryAddress, ProductSummary

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

SECRETS = (KEY_ID, API_KEY, ENTERPRISE_AB, SDN_BHD_AB)

ACTIVE_STATUS_BODY = {"active": True, "inactive": False, "discontinued": False}


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


def test_search_customers_isolates_each_company_account_book():
    captured = []

    def handler(request):
        captured.append(request)
        if request.url.path == f"/{ENTERPRISE_AB}/debtor/listing":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"},
                        {"AccNo": "E-0002", "CompanyName": "Enterprise Beta"},
                    ],
                    "totalCount": 2,
                },
            )
        if request.url.path == f"/{SDN_BHD_AB}/debtor/listing":
            return httpx.Response(
                200,
                json={
                    "data": [{"AccNo": "S-0001", "CompanyName": "Sdn Bhd Alpha"}],
                    "totalCount": 1,
                },
            )
        return httpx.Response(500, json={})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            enterprise = await adapter.search_customers(ENTERPRISE, "")
            sdn_bhd = await adapter.search_customers(SDN_BHD, "")
            return enterprise, sdn_bhd

        return _inner()

    enterprise, sdn_bhd = run(client, scenario)
    assert enterprise == [
        CustomerSummary(id="E-0001", code="E-0001", name="Enterprise Alpha"),
        CustomerSummary(id="E-0002", code="E-0002", name="Enterprise Beta"),
    ]
    assert sdn_bhd == [
        CustomerSummary(id="S-0001", code="S-0001", name="Sdn Bhd Alpha"),
    ]
    assert [r.url.path for r in captured] == [
        f"/{ENTERPRISE_AB}/debtor/listing",
        f"/{SDN_BHD_AB}/debtor/listing",
    ]


def test_search_customers_uses_documented_listing_request():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(SDN_BHD, "wanson")

        return _inner()

    run(client, scenario)
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == f"/{SDN_BHD_AB}/debtor/listing"
    assert request.url.params["page"] == "1"
    assert request.url.params["activeOnly"] == "true"


def _listing_pages(total, page_data):
    def handler(request):
        captured_page = request.url.params["page"]
        if captured_page not in page_data:
            return httpx.Response(200, json={"data": [], "totalCount": total})
        return httpx.Response(
            200,
            json={"data": page_data[captured_page], "totalCount": total},
        )

    return handler


def test_search_customers_consumes_all_pages_exactly_once_and_filters_locally():
    captured = []
    page_two = [
        {"AccNo": "C-SMITH-01", "CompanyName": "John Smith & Co"},
        {"AccNo": "C-SMITH-02", "CompanyName": "Smithsons Auto"},
    ]

    def handler(request):
        captured.append(request)
        page = request.url.params["page"]
        if page == "1":
            data = [
                {"AccNo": f"C-{i:04d}", "CompanyName": f"Client {i:04d}"}
                for i in range(100)
            ]
        else:
            data = page_two
        return httpx.Response(200, json={"data": data, "totalCount": 102})

    def search(query):
        client, adapter = make_adapter(handler)

        async def _inner():
            return await adapter.search_customers(ENTERPRISE, query)

        return run(client, _inner)

    assert search("SMITHSON") == [
        CustomerSummary(id="C-SMITH-02", code="C-SMITH-02", name="Smithsons Auto"),
    ]
    assert [r.url.params["page"] for r in captured] == ["1", "2"]
    assert all(r.url.params["activeOnly"] == "true" for r in captured)

    captured.clear()
    assert search("  c-0050  ") == [
        CustomerSummary(id="C-0050", code="C-0050", name="Client 0050"),
    ]
    assert [r.url.params["page"] for r in captured] == ["1", "2"]

    captured.clear()
    smith = search("smith")
    assert [c.id for c in smith] == ["C-SMITH-01", "C-SMITH-02"]

    captured.clear()
    everything = search("   ")
    assert len(everything) == 102
    assert [c.id for c in everything[:2]] == ["C-0000", "C-0001"]


def test_search_items_uses_documented_read_semantic_body():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": [], "totalCount": 0})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "shirt")

        return _inner()

    run(client, scenario)
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == f"/{ENTERPRISE_AB}/product/listing"
    assert json.loads(request.content) == {
        "page": 1,
        "filter": {"statuses": ACTIVE_STATUS_BODY},
    }


def test_search_items_increments_page_in_body():
    captured = []

    def handler(request):
        captured.append(request)
        page = json.loads(request.content)["page"]
        data = [
            {
                "product": {
                    "productCode": f"P-{(page - 1) * 100 + i:05d}",
                    "productName": f"Item {(page - 1) * 100 + i}",
                    "price": "10.00",
                }
            }
            for i in range(100 if page == 1 else 50)
        ]
        return httpx.Response(200, json={"data": data, "totalCount": 150})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    items = run(client, scenario)
    assert len(items) == 150
    assert [json.loads(r.content)["page"] for r in captured] == [1, 2]
    for request in captured:
        body = json.loads(request.content)
        assert body["filter"]["statuses"] == ACTIVE_STATUS_BODY


def test_search_items_normalizes_products_and_exact_decimal_prices():
    def handler(request):
        data = [
            {"product": {"productCode": "P-00001", "productName": "Shirt", "price": 350.0}},
            {"product": {"productCode": "P-00002", "productName": "Pants", "price": "12.50"}},
            {"product": {"productCode": "P-00003", "productName": "Hoodie", "price": 0}},
            {"product": {"productCode": "P-00004", "productName": "Cap", "price": 5}},
        ]
        return httpx.Response(200, json={"data": data, "totalCount": len(data)})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    items = run(client, scenario)
    assert items == [
        ProductSummary(id="P-00001", code="P-00001", name="Shirt", default_price=Decimal("350.0")),
        ProductSummary(id="P-00002", code="P-00002", name="Pants", default_price=Decimal("12.50")),
        ProductSummary(id="P-00003", code="P-00003", name="Hoodie", default_price=Decimal("0")),
        ProductSummary(id="P-00004", code="P-00004", name="Cap", default_price=Decimal("5")),
    ]
    assert items[1].default_price == Decimal("12.50")
    assert items[3].default_price == Decimal("5")


def test_get_delivery_addresses_returns_stable_default_address():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "accNo": "300-D001",
                "companyName": "Customer A",
                "deliverAddress": "116 Jalan Damai, Kuala Lumpur",
                "deliverPostCode": "55000",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    addresses = run(client, scenario)
    assert addresses == [
        DeliveryAddress(
            id="300-D001:delivery",
            label="Default delivery address",
            address_text="116 Jalan Damai, Kuala Lumpur\n55000",
        ),
    ]
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == f"/{ENTERPRISE_AB}/debtor"
    assert request.url.params["code"] == "300-D001"


def test_get_delivery_addresses_does_not_duplicate_existing_postcode():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "accNo": "300-D001",
                "deliverAddress": "116 Jalan Damai, Kuala Lumpur, 55000",
                "deliverPostCode": "55000",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    addresses = run(client, scenario)
    assert addresses == [
        DeliveryAddress(
            id="300-D001:delivery",
            label="Default delivery address",
            address_text="116 Jalan Damai, Kuala Lumpur, 55000",
        ),
    ]


def test_get_delivery_addresses_omits_blank_postcode():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "accNo": "300-D001",
                "deliverAddress": "116 Jalan Damai",
                "deliverPostCode": "",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    assert run(client, scenario) == [
        DeliveryAddress(
            id="300-D001:delivery",
            label="Default delivery address",
            address_text="116 Jalan Damai",
        ),
    ]


@pytest.mark.parametrize("deliver_address", [None, ""])
def test_get_delivery_addresses_blank_address_returns_empty(deliver_address):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "accNo": "300-D001",
                "deliverAddress": deliver_address,
                "deliverPostCode": "55000",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    assert run(client, scenario) == []


def test_get_delivery_addresses_blank_address_key_returns_empty():
    def handler(request):
        return httpx.Response(200, json={"accNo": "300-D001"})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    assert run(client, scenario) == []


def test_get_delivery_addresses_rejects_cross_customer_debtor():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "accNo": "300-D002",
                "companyName": "Customer B",
                "deliverAddress": "No. 1327, Jalan Padang Benggali",
                "deliverPostCode": "12000",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_get_delivery_addresses_accepts_documented_pascal_case_code():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "AccNo": "300-D001",
                "deliverAddress": "116 Jalan Damai",
                "deliverPostCode": "55000",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_delivery_addresses(ENTERPRISE, "300-D001")

        return _inner()

    assert run(client, scenario)[0].address_text == "116 Jalan Damai\n55000"


def test_get_item_lookup_and_code_verification():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "product": {
                    "productCode": "P-00001",
                    "productName": "Hoodie",
                    "price": "350.00",
                },
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_item(ENTERPRISE, "P-00001")

        return _inner()

    item = run(client, scenario)
    assert item == ProductSummary(
        id="P-00001",
        code="P-00001",
        name="Hoodie",
        default_price=Decimal("350.00"),
    )
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == f"/{ENTERPRISE_AB}/product"
    assert request.url.params["code"] == "P-00001"


def test_get_item_rejects_mismatched_product_code():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "product": {
                    "productCode": "P-99999",
                    "productName": "Other",
                    "price": "1.00",
                },
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.get_item(ENTERPRISE, "P-00001")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_empty_ids_and_non_string_query_rejected_before_transport():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            with pytest.raises(ValueError):
                await adapter.search_customers(ENTERPRISE, None)
            with pytest.raises(ValueError):
                await adapter.search_items(ENTERPRISE, 123)
            with pytest.raises(ValueError):
                await adapter.get_delivery_addresses(ENTERPRISE, "")
            with pytest.raises(ValueError):
                await adapter.get_delivery_addresses(ENTERPRISE, "   ")
            with pytest.raises(ValueError):
                await adapter.get_item(ENTERPRISE, "")
            with pytest.raises(ValueError):
                await adapter.get_item(ENTERPRISE, None)

        return _inner()

    run(client, scenario)
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"totalCount": 2},
        {"data": "not-a-list", "totalCount": 2},
        {"data": None, "totalCount": 2},
        [],
        "not-an-object",
    ],
)
def test_malformed_listing_envelopes_fail_closed(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


@pytest.mark.parametrize("total", [-1, True, "5", 2.5, None])
def test_malformed_totals_fail_closed(total):
    def handler(request):
        return httpx.Response(200, json={"data": [], "totalCount": total})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


@pytest.mark.parametrize(
    "row",
    [
        None,
        "not-a-dict",
        {},
        {"AccNo": "C-1"},
        {"CompanyName": "Only Name"},
        {"AccNo": 123, "CompanyName": "Numeric Code"},
        {"AccNo": "C-1", "CompanyName": ""},
        {"AccNo": "C-1", "accNo": "C-2", "CompanyName": "Name"},
    ],
)
def test_malformed_debtor_rows_fail_closed(row):
    def handler(request):
        return httpx.Response(200, json={"data": [row], "totalCount": 1})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_inconsistent_pagination_fails_closed_when_pages_run_short():
    def handler(request):
        page = request.url.params["page"]
        if page == "1":
            data = [
                {"AccNo": f"C-{i:03d}", "CompanyName": f"Client {i:03d}"}
                for i in range(100)
            ]
        else:
            data = []
        return httpx.Response(200, json={"data": data, "totalCount": 150})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_inconsistent_pagination_fails_closed_when_rows_exceed_total():
    def handler(request):
        data = [
            {"AccNo": f"C-{i}", "CompanyName": f"Client {i}"}
            for i in range(3)
        ]
        return httpx.Response(200, json={"data": data, "totalCount": 2})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_inconsistent_total_across_pages_fails_closed():
    captured = []

    def handler(request):
        captured.append(request)
        page = request.url.params["page"]
        if page == "1":
            data = [
                {"AccNo": f"C-{i:04d}", "CompanyName": f"Client {i:04d}"}
                for i in range(100)
            ]
            return httpx.Response(200, json={"data": data, "totalCount": 101})
        data = [{"AccNo": "C-0100", "CompanyName": "Client 0100"}]
        return httpx.Response(200, json={"data": data, "totalCount": 999})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)
    assert [r.url.params["page"] for r in captured] == ["1", "2"]


def test_repeated_customer_identity_across_pages_fails_closed():
    def handler(request):
        page = request.url.params["page"]
        if page == "1":
            data = [
                {"AccNo": f"C-{i:04d}", "CompanyName": f"Client {i:04d}"}
                for i in range(100)
            ]
        else:
            data = [{"AccNo": "C-0001", "CompanyName": "Repeated Row"}]
        return httpx.Response(200, json={"data": data, "totalCount": 101})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_repeated_product_identity_across_pages_fails_closed():
    def handler(request):
        page = json.loads(request.content)["page"]
        if page == 1:
            data = [
                {
                    "product": {
                        "productCode": f"P-{i:05d}",
                        "productName": f"Item {i}",
                        "price": "1.00",
                    }
                }
                for i in range(100)
            ]
        else:
            data = [
                {
                    "product": {
                        "productCode": "P-00001",
                        "productName": "Repeated Row",
                        "price": "1.00",
                    }
                }
            ]
        return httpx.Response(200, json={"data": data, "totalCount": 101})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_page_ceiling_stops_unbounded_listing_loop():
    captured = []

    def handler(request):
        captured.append(request)
        page = int(request.url.params["page"])
        data = [
            {
                "AccNo": f"C-PAGE-{page:04d}",
                "CompanyName": f"Client {page}",
            }
        ]
        return httpx.Response(200, json={"data": data, "totalCount": 2000})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)
    assert len(captured) == _LISTING_PAGE_LIMIT


@pytest.mark.parametrize("price", [-1, -0.01, True, False, None, "abc", "12.5x", []])
def test_unsafe_prices_fail_closed(price):
    def handler(request):
        data = [{"product": {"productCode": "P-00001", "productName": "X", "price": price}}]
        return httpx.Response(200, json={"data": data, "totalCount": 1})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"data": [{"product": {"productCode": "P-1", "productName": "X", "price": NaN}}], "totalCount": 1}',
        b'{"data": [{"product": {"productCode": "P-1", "productName": "X", "price": Infinity}}], "totalCount": 1}',
        b'{"data": [{"product": {"productCode": "P-1", "productName": "X", "price": -Infinity}}], "totalCount": 1}',
    ],
)
def test_nan_and_infinity_prices_fail_closed(raw_body):
    def handler(request):
        return httpx.Response(200, content=raw_body, headers={"content-type": "application/json"})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"product": None},
        {"product": "not-a-dict"},
        {"product": {}},
        {"product": {"productCode": "P-1", "productName": "X"}},
    ],
)
def test_malformed_product_rows_fail_closed(row):
    def handler(request):
        return httpx.Response(200, json={"data": [row], "totalCount": 1})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_items(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError):
        run(client, scenario)


def test_data_errors_do_not_leak_payloads_or_secrets():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"AccNo": "SECRET-CUST-CODE", "CompanyName": "Secret Taxpayer Ltd"}
                ],
                "totalCount": "not-an-int",
            },
        )

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            return await adapter.search_customers(ENTERPRISE, "")

        return _inner()

    with pytest.raises(AutoCountDataError) as exc:
        run(client, scenario)
    message = str(exc.value)
    for secret in (*SECRETS, "SECRET-CUST-CODE", "Secret Taxpayer Ltd"):
        assert secret not in message
        assert secret not in repr(exc.value)


_ADAPTER_CALLS = [
    lambda adapter: adapter.search_customers(ENTERPRISE, "wanson"),
    lambda adapter: adapter.search_items(ENTERPRISE, "shirt"),
    lambda adapter: adapter.get_delivery_addresses(ENTERPRISE, "300-D001"),
    lambda adapter: adapter.get_item(ENTERPRISE, "P-00001"),
]
_ADAPTER_CALL_IDS = [
    "search_customers",
    "search_items",
    "get_delivery_addresses",
    "get_item",
]


@pytest.mark.parametrize("call", _ADAPTER_CALLS, ids=_ADAPTER_CALL_IDS)
def test_rejected_status_propagates_unchanged(call):
    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            await call(adapter)

        return _inner()

    with pytest.raises(AutoCountRejectedError):
        run(client, scenario)


@pytest.mark.parametrize("call", _ADAPTER_CALLS, ids=_ADAPTER_CALL_IDS)
def test_transport_timeout_propagates_unchanged(call):
    def handler(request):
        raise httpx.ReadTimeout("read timed out")

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            await call(adapter)

        return _inner()

    with pytest.raises(AutoCountTransportError):
        run(client, scenario)


def test_adapter_always_routes_through_client_read():
    captured = []

    def handler(request):
        captured.append(request)
        path = request.url.path
        if path.endswith("/debtor/listing") or path.endswith("/product/listing"):
            return httpx.Response(200, json={"data": [], "totalCount": 0})
        if path.endswith("/debtor"):
            return httpx.Response(
                200,
                json={
                    "accNo": "300-D001",
                    "deliverAddress": "116 Jalan Damai",
                    "deliverPostCode": "55000",
                },
            )
        if path.endswith("/product"):
            return httpx.Response(
                200,
                json={"product": {"productCode": "P-00001", "productName": "X", "price": "1.00"}},
            )
        return httpx.Response(500, json={})

    client, adapter = make_adapter(handler)

    def scenario():
        async def _inner():
            await adapter.search_customers(ENTERPRISE, "")
            await adapter.search_items(SDN_BHD, "")
            await adapter.get_delivery_addresses(SDN_BHD, "300-D001")
            await adapter.get_item(ENTERPRISE, "P-00001")

        return _inner()

    run(client, scenario)
    assert [r.url.path for r in captured] == [
        f"/{ENTERPRISE_AB}/debtor/listing",
        f"/{SDN_BHD_AB}/product/listing",
        f"/{SDN_BHD_AB}/debtor",
        f"/{ENTERPRISE_AB}/product",
    ]
