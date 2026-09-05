"""Search-listing cache behaviour and debtor field projection.

The customer/item searches used to page the whole AutoCount listing on every
query (no documented server-side text search exists), which is what made the
mobile search feel slow. The adapter now caches each fully-consumed search
listing per account book for ``SEARCH_CACHE_TTL_SECONDS`` and filters per
query locally, and the debtor listing projects only the two fields search
renders. These tests pin:

- a fresh cache serves repeated and refined queries with one listing fetch;
- an expired (or zero-TTL) cache refetches;
- customers and products cache separately; account books never share a cache;
- identity reads (debtor detail) are never cached;
- a rejected listing caches nothing, so the retry is a real refetch;
- the debtor listing carries the documented ``field`` projection.
"""

import asyncio

import httpx

from app.autocount import AutoCountClient
from app.autocount.adapter import SEARCH_CACHE_TTL_SECONDS, AutoCountMasterDataAdapter
from app.config import CompanyConfig
from app.models.company import CompanyKey

KEY_ID = "key-id-42"
API_KEY = "api-key-secret-abc-123"
ENTERPRISE_AB = "ab-wanson-enterprise-001"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"

ENTERPRISE = CompanyConfig(
    key=CompanyKey.ENTERPRISE, name="Wanson Enterprise", account_book_id=ENTERPRISE_AB
)
SDN_BHD = CompanyConfig(
    key=CompanyKey.SDN_BHD, name="Wanson Enterprise (M) Sdn Bhd", account_book_id=SDN_BHD_AB
)


def make_adapter(handler, ttl_seconds=SEARCH_CACHE_TTL_SECONDS):
    transport = httpx.MockTransport(handler)
    client = AutoCountClient(KEY_ID, API_KEY, transport=transport)
    return client, AutoCountMasterDataAdapter(
        client, search_cache_ttl_seconds=ttl_seconds
    )


def run(client, coro_fn):
    async def _run():
        try:
            return await coro_fn()
        finally:
            await client.aclose()

    return asyncio.run(_run())


def debtor_listing_response(rows):
    return httpx.Response(200, json={"data": rows, "totalCount": len(rows)})


def debtor_detail_response(acc_no, name):
    return httpx.Response(
        200, json={"accNo": acc_no, "companyName": name, "taxEntity": None}
    )


def product_listing_response():
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "product": {
                        "productCode": "P-0001",
                        "productName": "Widget",
                        "price": "12.50",
                        "classificationCode": None,
                    }
                }
            ],
            "totalCount": 1,
        },
    )


def test_second_customer_search_within_ttl_does_not_refetch():
    listing_calls = []

    def handler(request):
        listing_calls.append(request)
        return debtor_listing_response(
            [
                {"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"},
                {"AccNo": "E-0002", "CompanyName": "Alpha Steel"},
            ]
        )

    client, adapter = make_adapter(handler)

    async def _flow():
        first = await adapter.search_customers(ENTERPRISE, "alpha")
        second = await adapter.search_customers(ENTERPRISE, "steel")
        refined = await adapter.search_customers(ENTERPRISE, "")
        return first, second, refined

    first, second, refined = run(client, _flow)

    assert len(listing_calls) == 1
    assert [c.code for c in first] == ["E-0001", "E-0002"]
    assert [c.code for c in second] == ["E-0002"]
    assert [c.code for c in refined] == ["E-0001", "E-0002"]


def test_search_refetches_once_the_ttl_expires():
    listing_calls = []

    def handler(request):
        listing_calls.append(request)
        return debtor_listing_response(
            [{"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"}]
        )

    # A zero TTL means the cache never serves: every search is a real fetch.
    client, adapter = make_adapter(handler, ttl_seconds=0)

    async def _flow():
        await adapter.search_customers(ENTERPRISE, "alpha")
        await adapter.search_customers(ENTERPRISE, "alpha")

    run(client, _flow)

    assert len(listing_calls) == 2


def test_customers_and_products_cache_separately():
    debtor_calls = []
    product_calls = []

    def handler(request):
        if request.url.path.endswith("/debtor/listing"):
            debtor_calls.append(request)
            return debtor_listing_response(
                [{"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"}]
            )
        if request.url.path.endswith("/product/listing"):
            product_calls.append(request)
            return product_listing_response()
        return httpx.Response(500, json={})

    client, adapter = make_adapter(handler)

    async def _flow():
        customers_a = await adapter.search_customers(ENTERPRISE, "alpha")
        items_a = await adapter.search_items(ENTERPRISE, "widget")
        customers_b = await adapter.search_customers(ENTERPRISE, "")
        items_b = await adapter.search_items(ENTERPRISE, "")
        return customers_a, items_a, customers_b, items_b

    customers_a, items_a, customers_b, items_b = run(client, _flow)

    assert len(debtor_calls) == 1
    assert len(product_calls) == 1
    assert customers_a == customers_b
    assert items_a == items_b


def test_search_cache_never_bridges_account_books():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == f"/{ENTERPRISE_AB}/debtor/listing":
            return debtor_listing_response(
                [{"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"}]
            )
        if request.url.path == f"/{SDN_BHD_AB}/debtor/listing":
            return debtor_listing_response(
                [{"AccNo": "S-0001", "CompanyName": "Sdn Bhd Alpha"}]
            )
        return httpx.Response(500, json={})

    client, adapter = make_adapter(handler)

    async def _flow():
        enterprise = await adapter.search_customers(ENTERPRISE, "alpha")
        sdn_bhd = await adapter.search_customers(SDN_BHD, "alpha")
        enterprise_again = await adapter.search_customers(ENTERPRISE, "")
        return enterprise, sdn_bhd, enterprise_again

    enterprise, sdn_bhd, enterprise_again = run(client, _flow)

    assert [c.code for c in enterprise] == ["E-0001"]
    assert [c.code for c in sdn_bhd] == ["S-0001"]
    assert [c.code for c in enterprise_again] == ["E-0001"]
    assert len(calls) == 2


def test_debtor_detail_read_is_never_cached():
    detail_calls = []

    def handler(request):
        detail_calls.append(request)
        return debtor_detail_response("300-D001", "Customer A")

    client, adapter = make_adapter(handler)

    async def _flow():
        first = await adapter.get_customer(ENTERPRISE, "300-D001")
        second = await adapter.get_customer(ENTERPRISE, "300-D001")
        return first, second

    first, second = run(client, _flow)

    assert first == second
    assert len(detail_calls) == 2


def test_rejected_listing_caches_nothing():
    listing_calls = []

    def handler(request):
        listing_calls.append(request)
        if len(listing_calls) == 1:
            return httpx.Response(502, json={"statusCode": 502, "message": "upstream"})
        return debtor_listing_response(
            [{"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"}]
        )

    client, adapter = make_adapter(handler)

    async def _flow():
        from app.autocount.errors import AutoCountRejectedError

        try:
            await adapter.search_customers(ENTERPRISE, "alpha")
        except AutoCountRejectedError:
            pass
        else:
            raise AssertionError("first search should propagate the rejection")
        return await adapter.search_customers(ENTERPRISE, "alpha")

    result = run(client, _flow)

    assert [c.code for c in result] == ["E-0001"]
    assert len(listing_calls) == 2


def test_debtor_listing_projects_only_the_search_fields():
    listing_calls = []

    def handler(request):
        listing_calls.append(request)
        return debtor_listing_response(
            [{"AccNo": "E-0001", "CompanyName": "Enterprise Alpha"}]
        )

    client, adapter = make_adapter(handler)

    async def _flow():
        return await adapter.search_customers(ENTERPRISE, "alpha")

    run(client, _flow)

    request = listing_calls[0]
    assert request.url.params.get_list("field") == ["accNo", "companyName"]
    assert request.url.params["page"] == "1"
    assert request.url.params["activeOnly"] == "true"
