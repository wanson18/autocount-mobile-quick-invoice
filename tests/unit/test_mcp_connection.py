"""Security regression tests for the additional read-only MCP surface."""
import pytest
from fastapi.testclient import TestClient
from app.main import app


def test_unconfigured_mcp_is_never_anonymous():
    with TestClient(app) as client:
        response = client.post('/invoice-mcp/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
        })
        assert response.status_code == 401
        assert 'resource_metadata' in response.headers['www-authenticate']


def test_google_setup_failure_does_not_break_original_api():
    with TestClient(app) as client:
        assert client.get('/openapi.json').status_code == 200
        assert client.get('/').status_code == 200
        assert client.get('/invoice-mcp/authorize').status_code == 503


@pytest.mark.asyncio
async def test_verified_owner_only():
    from app.mcp_connection import owner_allowed
    assert owner_allowed({'email': 'wanson18@gmail.com', 'email_verified': True}, {'wanson18@gmail.com'})
    for claims in [
        {'email': 'someone@gmail.com', 'email_verified': True},
        {'email': 'wanson18@gmail.com', 'email_verified': False},
        {'email': 'wanson18@gmail.com', 'email_verified': 'false'},
        {},
    ]:
        assert not owner_allowed(claims, {'wanson18@gmail.com'})


def test_mcp_company_has_no_shared_credential_fallback(monkeypatch):
    from app.mcp_connection import resolve_mcp_company
    from app.models.company import CompanyKey
    monkeypatch.setenv('AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE', 'enterprise-book')
    monkeypatch.setenv('AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD', 'sdn-book')
    for key in ['AUTOCOUNT_KEY_ID_WANSON_ENTERPRISE', 'AUTOCOUNT_API_KEY_WANSON_ENTERPRISE']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AUTOCOUNT_API_KEY_ID', 'legacy-key')
    monkeypatch.setenv('AUTOCOUNT_API_KEY', 'legacy-secret')
    with pytest.raises(ValueError, match='company-specific'):
        resolve_mcp_company(CompanyKey.ENTERPRISE)


@pytest.mark.asyncio
async def test_real_mcp_protocol_is_authenticated_read_only_and_company_isolated(monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.auth import AccessToken, TokenVerifier
    from app import mcp_connection
    from app.models.master_data import CustomerSummary

    # A fake upstream is required so this regression test never reads live accounts.
    class Master:
        async def search_customers(self, company, q):
            return [CustomerSummary(company.key.value, company.key.value,
                                    'Seasons ' + company.key.value)]

    class TestAuth(TokenVerifier):
        async def verify_token(self, token):
            if token == 'test-owner':
                return AccessToken(token=token, client_id='test', scopes=[])
            return None

    for suffix, book in [('ENTERPRISE', 'enterprise-book'), ('SDN_BHD', 'sdn-book')]:
        monkeypatch.setenv('AUTOCOUNT_ACCOUNT_BOOK_WANSON_' + suffix, book)
        monkeypatch.setenv('AUTOCOUNT_KEY_ID_WANSON_' + suffix, 'key-' + suffix)
        monkeypatch.setenv('AUTOCOUNT_API_KEY_WANSON_' + suffix, 'secret-' + suffix)
    monkeypatch.setattr(mcp_connection, 'get_master_data', lambda: Master())
    server = mcp_connection.build_lookup_server(TestAuth())
    http_app = server.http_app(path='/mcp', json_response=True, stateless_http=True)
    outer = Starlette(routes=[Mount('/invoice-mcp', http_app)], lifespan=http_app.lifespan)
    async with http_app.lifespan(outer):
        factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.ASGITransport(app=outer), **kwargs)
        transport = StreamableHttpTransport('http://testserver/invoice-mcp/mcp',
                                           headers={'Authorization': 'Bearer test-owner'},
                                           httpx_client_factory=factory)
        async with Client(transport) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools} == {'listCompanies', 'searchCustomers',
                'listCustomerAddresses', 'searchProducts', 'previewInvoicePrices'}
            assert all(t.annotations.read_only_hint for t in tools)
            for company in ['enterprise', 'sdn_bhd']:
                result = await client.call_tool('searchCustomers', {'company': company, 'q': 'Seasons'})
                assert result.data['company'] == company
                assert result.data['data'][0]['code'] == company
                assert 'enterprise-book' not in str(result.data)
                assert 'sdn-book' not in str(result.data)
            for arguments in [{'company': 'sdnbhd', 'q': 'Seasons'},
                              {'company': 'enterprise', 'q': 'Seasons', 'account_book_id': 'sdn-book'},
                              {'q': 'Seasons'}]:
                with pytest.raises(Exception):
                    await client.call_tool('searchCustomers', arguments)
        async with factory() as http:
            denied = await http.post('http://testserver/invoice-mcp/mcp',
                                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
            assert denied.status_code == 401


def test_configured_oauth_discovery_and_origin_guard(monkeypatch):
    from app.mcp_connection import build_mcp_app, REQUIRED_SETTINGS, AUTH_BASE, MCP_URL
    for key in REQUIRED_SETTINGS:
        monkeypatch.setenv(key, 'a' * 40)
    monkeypatch.setenv('INVOICE_MCP_ALLOWED_EMAILS', 'wanson18@gmail.com')
    monkeypatch.setenv('POSTGRES_URL', 'postgresql://not-used-in-this-test/db')
    configured = build_mcp_app()
    from starlette.applications import Starlette
    from starlette.routing import Mount
    outer = Starlette(routes=[Mount('/invoice-mcp', configured)], lifespan=configured.lifespan)
    with TestClient(outer, base_url='https://autocount-mobile-quick-invoice.vercel.app') as client:
        metadata = client.get('/invoice-mcp/.well-known/oauth-authorization-server').json()
        assert metadata['issuer'] == AUTH_BASE
        assert metadata['authorization_endpoint'] == AUTH_BASE + '/authorize'
        assert metadata['token_endpoint'] == AUTH_BASE + '/token'
        assert metadata['code_challenge_methods_supported'] == ['S256']
        resource = client.get('/invoice-mcp/.well-known/oauth-protected-resource/invoice-mcp/mcp').json()
        assert resource['resource'] == MCP_URL
        response = client.post('/invoice-mcp/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                               headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403
        assert client.post('/invoice-mcp/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code == 401
