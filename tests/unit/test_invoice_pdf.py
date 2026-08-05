"""Task 9 - official invoice PDF contract.

The technical spike (``docs/autocount/pdf-spike.md``) confirmed the documented
Cloud Accounting Integration API exposes no PDF, print, or download mechanism.
The adapter contract must therefore fail closed with
``AutoCountUnsupportedError`` and never touch the network, so sharing stays
disabled.

When AutoCount documents a supported endpoint, replace the raise in
``AutoCountMasterDataAdapter.get_invoice_pdf`` and extend this file with the
intended contract tests: the documented request is sent through
``AutoCountClient.read``, and returned bytes start with ``%PDF``.
"""

import asyncio

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.autocount.errors import AutoCountUnsupportedError
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


def test_get_invoice_pdf_is_unsupported_without_any_network_call():
    def handler(request):  # pragma: no cover - the adapter must never call it
        raise AssertionError(f"no network call expected, got {request.url}")

    client, adapter = make_adapter(handler)

    def call():
        return adapter.get_invoice_pdf(SDN_BHD, "9001")

    with pytest.raises(AutoCountUnsupportedError, match="no documented"):
        run(client, call)
    assert client.is_closed  # closed cleanly after the fail-closed raise


def test_get_invoice_pdf_error_is_safe_to_surface():
    client, adapter = make_adapter(lambda request: httpx.Response(200))

    def call():
        return adapter.get_invoice_pdf(SDN_BHD, "9001")

    with pytest.raises(AutoCountUnsupportedError) as exc_info:
        run(client, call)
    message = str(exc_info.value)
    assert KEY_ID not in message
    assert API_KEY not in message
    assert SDN_BHD_AB not in message


def test_get_invoice_pdf_rejects_blank_invoice_id_before_anything_else():
    client, adapter = make_adapter(lambda request: httpx.Response(200))

    def call():
        return adapter.get_invoice_pdf(SDN_BHD, "   ")

    with pytest.raises(ValueError, match="invoice_id"):
        run(client, call)


@pytest.mark.parametrize("invoice_id", [None, 123, b"9001"])
def test_get_invoice_pdf_rejects_non_string_invoice_id(invoice_id):
    client, adapter = make_adapter(lambda request: httpx.Response(200))

    def call():
        return adapter.get_invoice_pdf(SDN_BHD, invoice_id)

    with pytest.raises(ValueError, match="invoice_id"):
        run(client, call)
