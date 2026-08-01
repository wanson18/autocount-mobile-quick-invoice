"""Task 3 — AutoCount Cloud Integration API transport boundary.

Behaviour proven through ``httpx.MockTransport`` (no real network):

- mandatory ``API-Key`` and ``Key-ID`` headers on every request;
- JSON bodies get an appropriate Content-Type;
- each company routes to its own server-selected account-book path;
- callers cannot override credentials or account-book routing;
- configured timeout reaches the request and the default is finite;
- non-2xx statuses normalise to ``AutoCountRejectedError``;
- a write timeout is ambiguous while a read timeout is a transport failure;
- error ``str``/``repr`` never leak the API key, Key ID, or account-book IDs;
- the async client closes cleanly.
"""

import asyncio

import httpx
import pytest

from app.autocount import DEFAULT_TIMEOUT, AutoCountClient, RequestOperation
from app.autocount.errors import (
    AutoCountAmbiguousWriteError,
    AutoCountConfigError,
    AutoCountEndpointError,
    AutoCountRejectedError,
    AutoCountTransportError,
)
from app.config import CompanyConfig
from app.models.company import CompanyKey

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


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return AutoCountClient(KEY_ID, API_KEY, transport=transport, **kwargs)


def autoclose(client, coro_fn):
    """Run an async scenario and always close the client afterwards."""

    async def _run():
        try:
            return await coro_fn()
        finally:
            await client.aclose()

    return asyncio.run(_run())


def test_auth_headers_attached_and_json_content_type():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)

    async def scenario():
        return await client.read(ENTERPRISE, "POST", "account/listing", json={})

    response = autoclose(client, scenario)
    assert response.status_code == 200
    request = captured[0]
    assert request.headers["Key-ID"] == KEY_ID
    assert request.headers["API-Key"] == API_KEY
    assert request.headers["content-type"].startswith("application/json")


def test_each_company_uses_its_own_account_book_path():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        await client.read(ENTERPRISE, "POST", "account/listing", json={})
        await client.read(SDN_BHD, "POST", "debtor/listing", json={})

    autoclose(client, scenario)
    assert paths == [f"/{ENTERPRISE_AB}/account/listing", f"/{SDN_BHD_AB}/debtor/listing"]


def test_default_base_url_host_is_used():
    captured = []

    def handler(request):
        captured.append(request.url.host)
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    assert captured[0] == "accounting-api.autocountcloud.com"


def test_leading_slash_on_endpoint_is_normalised():
    captured = []

    def handler(request):
        captured.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        await client.read(ENTERPRISE, "GET", "/account/listing")

    autoclose(client, scenario)
    assert captured[0] == f"/{ENTERPRISE_AB}/account/listing"


@pytest.mark.parametrize(
    "endpoint",
    [
        "%2e%2e/admin",
        "%2E%2E/admin",
        "a/%2e%2e/admin",
        "%252e%252e/admin",
        "%2fadmin",
        "%2Fadmin",
        "%5cadmin",
        "%5Cadmin",
        "a%20b",
        "a b",
        "a\tb",
        "a\nb",
        "a\x00b",
        "a\x7fb",
    ],
)
def test_escaped_or_control_paths_rejected_before_transport(endpoint):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountEndpointError):
            await client.read(ENTERPRISE, "GET", endpoint)
        with pytest.raises(AutoCountEndpointError):
            await client.write(ENTERPRISE, "POST", endpoint, json={})

    autoclose(client, scenario)
    assert calls == []


def test_query_values_pass_through_params():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        await client.read(
            ENTERPRISE, "GET", "account/listing", params={"page": 1, "searchText": "cash"}
        )

    autoclose(client, scenario)
    request = captured[0]
    assert request.url.path == f"/{ENTERPRISE_AB}/account/listing"
    assert request.url.params["page"] == "1"
    assert request.url.params["searchText"] == "cash"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/api",
        "http://evil.example/api",
        "//evil.example/api",
        "/../etc/passwd",
        "../account/listing",
        "account/../listing",
        "account/./listing",
        "account/listing#frag",
        "account/listing?page=1",
        "account\\listing",
        "",
        "   ",
    ],
)
def test_unsafe_endpoints_are_rejected(endpoint):
    client = make_client(lambda request: httpx.Response(200, json={}))

    async def scenario():
        with pytest.raises(AutoCountEndpointError):
            await client.read(ENTERPRISE, "GET", endpoint)

    autoclose(client, scenario)


def test_callers_cannot_override_credentials():
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(TypeError):
        client.read(ENTERPRISE, "GET", "x", headers={"API-Key": "attacker-key"})
    with pytest.raises(TypeError):
        client.read(ENTERPRISE, "GET", "x", api_key="attacker-key")
    with pytest.raises(TypeError):
        client.read(ENTERPRISE, "GET", "x", key_id="attacker-key")
    asyncio.run(client.aclose())


def test_callers_cannot_override_account_book_routing():
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(TypeError):
        client.read(ENTERPRISE, "GET", "x", account_book_id="attacker-book")

    async def scenario():
        with pytest.raises(AutoCountConfigError):
            await client.read("enterprise", "GET", "x")

    autoclose(client, scenario)


def test_configured_timeout_reaches_request():
    captured = []

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = make_client(handler, timeout=httpx.Timeout(5.0))

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    assert captured[0] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


def test_default_timeout_is_finite():
    captured = []

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = make_client(handler)

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    timeout = captured[0]
    for name in ("connect", "read", "write", "pool"):
        value = timeout[name]
        assert value is not None
        assert value > 0


def test_none_timeout_falls_back_to_safe_default():
    captured = []

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = make_client(handler, timeout=None)

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    assert captured[0] == DEFAULT_TIMEOUT.as_dict()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
def test_non_2xx_statuses_normalise_to_rejected_error(status):
    def handler(request):
        return httpx.Response(status, json={"statusCode": status, "message": "rejected by AutoCount"})

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(ENTERPRISE, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    assert exc.value.status_code == status
    assert "rejected by AutoCount" in str(exc.value)


def test_redirect_status_is_treated_as_rejection():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(ENTERPRISE, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    assert exc.value.status_code == 302


def test_write_timeout_is_ambiguous():
    def handler(request):
        raise httpx.ReadTimeout("read timed out")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountAmbiguousWriteError) as exc:
            await client.write(ENTERPRISE, "POST", "invoice/save", json={})
        return exc

    exc = autoclose(client, scenario)
    assert "timed out" in str(exc.value)


def test_connect_timeout_during_write_is_ambiguous():
    def handler(request):
        raise httpx.ConnectTimeout("connect timed out")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountAmbiguousWriteError):
            await client.write(ENTERPRISE, "POST", "invoice/save", json={})

    autoclose(client, scenario)


def test_read_timeout_is_ordinary_transport_failure():
    def handler(request):
        raise httpx.ReadTimeout("read timed out")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountTransportError) as exc:
            await client.read(ENTERPRISE, "POST", "account/listing", json={})
        return exc

    exc = autoclose(client, scenario)
    assert "timed out" in str(exc.value)


def test_other_request_errors_normalise_to_transport_error():
    def handler(request):
        raise httpx.ConnectError("Connection refused: https://evil.example/secret-token")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountTransportError) as exc:
            await client.read(ENTERPRISE, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    assert "https://evil.example/secret-token" not in str(exc.value)


def test_rejection_message_is_redacted_when_autocount_echoes_secrets():
    def handler(request):
        body = {
            "statusCode": 401,
            "message": f"unauthorized {API_KEY} / {KEY_ID} / {ENTERPRISE_AB}",
        }
        return httpx.Response(401, json=body)

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(ENTERPRISE, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    err = exc.value
    for secret in SECRETS:
        assert secret not in str(err)
        assert secret not in repr(err)
    assert "<redacted>" in str(err)
    assert "<redacted>" in repr(err)


def test_sdn_bhd_rejection_message_is_redacted():
    def handler(request):
        body = {"statusCode": 403, "message": f"forbidden for {SDN_BHD_AB} {API_KEY}"}
        return httpx.Response(403, json=body)

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(SDN_BHD, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    err = exc.value
    for secret in SECRETS:
        assert secret not in str(err)
        assert secret not in repr(err)


def test_transport_error_repr_is_safe():
    def handler(request):
        raise httpx.ReadTimeout(f"read timed out on {API_KEY}/{KEY_ID}/{ENTERPRISE_AB}")

    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountTransportError) as exc:
            await client.read(ENTERPRISE, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    err = exc.value
    for secret in SECRETS:
        assert secret not in str(err)
        assert secret not in repr(err)


def test_mock_transport_used_no_real_network():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)

    async def scenario():
        response = await client.read(ENTERPRISE, "POST", "account/listing", json={})
        return response.json()

    result = autoclose(client, scenario)
    assert result == {"ok": True}
    assert len(calls) == 1


def test_client_closes_cleanly_and_idempotently():
    client = make_client(lambda request: httpx.Response(200, json={}))
    assert client.is_closed is False
    asyncio.run(client.aclose())
    assert client.is_closed is True
    asyncio.run(client.aclose())
    assert client.is_closed is True


def test_context_manager_closes_client():
    client = None

    async def scenario():
        nonlocal client
        async with make_client(lambda request: httpx.Response(200, json={})) as opened:
            client = opened
            assert client.is_closed is False
            await opened.read(ENTERPRISE, "GET", "x")

    asyncio.run(scenario())
    assert client.is_closed is True


def test_request_requires_explicit_operation_marker():
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(TypeError):
        client.request(ENTERPRISE, "GET", "x")

    async def scenario():
        with pytest.raises(AutoCountConfigError):
            await client.request(ENTERPRISE, "GET", "x", operation="read")

    autoclose(client, scenario)


@pytest.mark.parametrize(
    "overrides",
    [
        {"key_id": ""},
        {"api_key": ""},
        {"key_id": "   "},
        {"api_key": "   "},
    ],
)
def test_empty_credentials_rejected_at_construction(overrides):
    kwargs = {"key_id": KEY_ID, "api_key": API_KEY}
    kwargs.update(overrides)
    with pytest.raises(AutoCountConfigError):
        AutoCountClient(**kwargs)


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        "5",
        {},
        httpx.Timeout(0),
        httpx.Timeout(-1),
        httpx.Timeout(float("nan")),
        httpx.Timeout(float("inf")),
        httpx.Timeout(None),
        httpx.Timeout(5.0, read=None),
        httpx.Timeout(5.0, connect=None),
    ],
)
def test_unsafe_timeouts_rejected_at_construction(timeout):
    with pytest.raises(AutoCountConfigError):
        AutoCountClient(
            KEY_ID,
            API_KEY,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            timeout=timeout,
        )


def test_integer_timeout_accepted_and_reaches_request():
    captured = []

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = make_client(handler, timeout=7)

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    assert captured[0] == {"connect": 7, "read": 7, "write": 7, "pool": 7}


def test_custom_timeout_components_reach_request():
    captured = []

    def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = make_client(handler, timeout=httpx.Timeout(1.5, connect=2.0, read=3.0))

    async def scenario():
        await client.read(ENTERPRISE, "GET", "x")

    autoclose(client, scenario)
    assert captured[0] == {"connect": 2.0, "read": 3.0, "write": 1.5, "pool": 1.5}


def _fragments(text):
    for i in range(len(text)):
        for j in range(i + 2, len(text) + 1):
            yield text[i:j]


def _assert_clean(error, secrets):
    message = str(error)
    for secret in secrets:
        assert secret not in message
        for arg in error.args:
            assert secret not in arg
        for fragment in _fragments(secret):
            assert fragment not in message
            for arg in error.args:
                assert fragment not in arg
    expected_repr = f"{type(error).__name__}({error.status_code}, {message!r})"
    assert repr(error) == expected_repr


def test_redaction_handles_overlapping_secrets_longest_first():
    key_id = "K1"
    api_key = "K1-KEY-0007"
    account_book = "K1-KEY-0007-BOOK"
    company = CompanyConfig(key=CompanyKey.ENTERPRISE, name="Wanson Enterprise", account_book_id=account_book)

    def handler(request):
        return httpx.Response(400, json={"message": f"echo {api_key} / {key_id} / {account_book}"})

    client = AutoCountClient(key_id, api_key, transport=httpx.MockTransport(handler))

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(company, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    _assert_clean(exc.value, (key_id, api_key, account_book))


def test_redaction_covers_credential_straddling_cap_boundary():
    api_key = "Z9-SECRET-9001"
    prefix = "p" * 495
    full_message = prefix + api_key + "z" * 100
    assert len(full_message) > 500
    assert full_message[:500].endswith(api_key[:5])
    company = CompanyConfig(key=CompanyKey.ENTERPRISE, name="Wanson Enterprise", account_book_id="Z9-BOOK-1")

    def handler(request):
        return httpx.Response(400, json={"message": full_message})

    client = AutoCountClient("Q1", api_key, transport=httpx.MockTransport(handler))

    async def scenario():
        with pytest.raises(AutoCountRejectedError) as exc:
            await client.read(company, "GET", "x")
        return exc

    exc = autoclose(client, scenario)
    assert len(str(exc.value)) <= 500
    _assert_clean(exc.value, (api_key, "Z9-BOOK-1", "Q1"))


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        " ",
        "ab/id",
        "ab\\id",
        "ab?id",
        "ab#id",
        "ab%id",
        "ab.id",
        "..",
        ".",
        "ab id",
        "ab\nid",
        None,
        123,
    ],
)
def test_malformed_account_book_ids_fail_closed(bad_id):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    company = CompanyConfig(key=CompanyKey.ENTERPRISE, name="Wanson Enterprise", account_book_id=bad_id)
    client = make_client(handler)

    async def scenario():
        with pytest.raises(AutoCountConfigError):
            await client.read(company, "GET", "x")
        with pytest.raises(AutoCountConfigError):
            await client.write(company, "POST", "y", json={})

    autoclose(client, scenario)
    assert calls == []


def test_legitimate_account_book_ids_are_preserved_verbatim():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    for book_id in ("AB_123-xyz", "a1B2_C3", "simple"):
        company = CompanyConfig(key=CompanyKey.ENTERPRISE, name="Wanson Enterprise", account_book_id=book_id)
        client = make_client(handler)

        async def scenario():
            await client.read(company, "GET", "x")

        autoclose(client, scenario)

    assert calls == ["/AB_123-xyz/x", "/a1B2_C3/x", "/simple/x"]
