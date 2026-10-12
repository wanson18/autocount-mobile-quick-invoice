"""Additional read-only MCP service; the existing REST API stays independent.

OAuth configuration is server-only. Missing configuration denies MCP access,
without disabling the established mobile invoice application.
"""
from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import get_company
from app.dependencies import get_master_data
from app.models.company import CompanyKey
from app.models.invoice import InvoicePreviewInput
from app.models.master_data import CustomerSearchItem, DeliveryAddressItem, ProductSearchItem
from app.api.invoices import preview_invoice_prices

PUBLIC_BASE = 'https://autocount-mobile-quick-invoice.vercel.app'
MOUNT_PATH = '/invoice-mcp'
AUTH_BASE = PUBLIC_BASE + MOUNT_PATH
MCP_URL = AUTH_BASE + '/mcp'
REQUIRED_SETTINGS = (
    'INVOICE_MCP_GOOGLE_CLIENT_ID', 'INVOICE_MCP_GOOGLE_CLIENT_SECRET',
    'INVOICE_MCP_ALLOWED_EMAILS', 'INVOICE_MCP_SIGNING_KEY',
)


def owner_allowed(claims: dict[str, Any], allowed: set[str]) -> bool:
    # Google returns boolean or the string "true". Never truth-test "false".
    verified = claims.get('email_verified')
    email = claims.get('email')
    return (verified is True or verified == 'true') and isinstance(email, str) and email.casefold() in allowed


def resolve_mcp_company(company: CompanyKey):
    selected = get_company(company)
    if not selected.key_id or not selected.api_key:
        raise ValueError('MCP requires company-specific AutoCount credentials')
    return selected


def build_lookup_server(auth):
    """Construct only the approved reads, reusing the existing adapter."""
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    server = FastMCP('Wanson Invoice Generation lookups', auth=auth, mask_error_details=True)
    annotations = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': True}

    def selected(company):
        try:
            return resolve_mcp_company(company)
        except Exception:
            raise ToolError('Selected company configuration is unavailable; no other company was used.') from None

    @server.tool(name='listCompanies', annotations=annotations)
    async def list_companies() -> dict:
        return {'data': [{'key': 'enterprise', 'name': 'Wanson Enterprise'},
                         {'key': 'sdn_bhd', 'name': 'Wanson Enterprise (M) Sdn Bhd'}]}

    @server.tool(name='searchCustomers', annotations=annotations)
    async def search_customers(company: CompanyKey, q: Annotated[str, Field(min_length=1, max_length=100)]) -> dict:
        """Search customers separately in the explicitly selected company's book."""
        rows = await get_master_data().search_customers(selected(company), q)
        return {'company': company.value, 'data': [CustomerSearchItem.model_validate(r, from_attributes=True).model_dump(mode='json') for r in rows]}

    @server.tool(name='listCustomerAddresses', annotations=annotations)
    async def list_customer_addresses(company: CompanyKey, customer_id: Annotated[str, Field(min_length=1, max_length=100)]) -> dict:
        """Read delivery addresses from this company's customer master only."""
        rows = await get_master_data().get_delivery_addresses(selected(company), customer_id)
        return {'company': company.value, 'data': [DeliveryAddressItem.model_validate(r, from_attributes=True).model_dump(mode='json') for r in rows]}

    @server.tool(name='searchProducts', annotations=annotations)
    async def search_products(company: CompanyKey, q: Annotated[str, Field(min_length=1, max_length=100)]) -> dict:
        """Search products in the selected company; prices are exact decimal strings."""
        rows = await get_master_data().search_items(selected(company), q)
        return {'company': company.value, 'data': [ProductSearchItem.model_validate(r, from_attributes=True).model_dump(mode='json') for r in rows]}

    @server.tool(name='previewInvoicePrices', annotations=annotations)
    async def preview_prices(preview: InvoicePreviewInput) -> dict:
        """Read historical prices. This is advisory and does not issue an invoice."""
        selected(preview.company)
        result = await preview_invoice_prices(preview, master=get_master_data())
        return {'company': preview.company.value, **result.model_dump(mode='json')}

    return server


def build_mcp_app():
    """Return authenticated HTTP app or a closed setup-pending surface."""
    settings = {key: os.environ.get(key, '').strip() for key in REQUIRED_SETTINGS}
    database = os.environ.get('POSTGRES_URL') or os.environ.get('DATABASE_URL')
    if not all(settings.values()) or not database or len(settings['INVOICE_MCP_SIGNING_KEY']) < 32:
        async def denied(request):
            return JSONResponse({'error': 'authentication_required'}, status_code=401,
                headers={'WWW-Authenticate': f'Bearer resource_metadata="{PUBLIC_BASE}/.well-known/oauth-protected-resource/invoice-mcp/mcp"', 'Cache-Control': 'no-store'})

        async def pending(request):
            return JSONResponse({'error': 'google_sign_in_setup_pending'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})

        return Starlette(routes=[Route('/mcp', denied, methods=['GET', 'POST', 'DELETE']),
                                 Route('/{path:path}', pending, methods=['GET', 'POST'])])

    from fastmcp.server.auth import OAuthProxy
    from fastmcp.server.auth.providers.google import GoogleTokenVerifier
    from key_value.aio.stores.postgresql import PostgreSQLStore
    from key_value.aio.wrappers.encryption.fernet import FernetEncryptionWrapper
    from cryptography.fernet import Fernet
    import base64
    import hashlib

    allowed = {email.strip().casefold() for email in settings['INVOICE_MCP_ALLOWED_EMAILS'].split(',') if email.strip()}
    scopes = ['openid', 'https://www.googleapis.com/auth/userinfo.email']

    class OwnerGoogleVerifier(GoogleTokenVerifier):
        async def verify_token(self, token):
            result = await super().verify_token(token)
            if result is None or not owner_allowed(result.claims, allowed):
                return None
            return result

    storage_key = base64.urlsafe_b64encode(hashlib.sha256(
        ('wanson-invoice-mcp-storage-v1:' + settings['INVOICE_MCP_SIGNING_KEY']).encode()).digest())
    storage = FernetEncryptionWrapper(
        key_value=PostgreSQLStore(url=database, table_name='invoice_mcp_oauth'),
        fernet=Fernet(storage_key),
    )
    auth = OAuthProxy(
        upstream_authorization_endpoint='https://accounts.google.com/o/oauth2/v2/auth',
        upstream_token_endpoint='https://oauth2.googleapis.com/token',
        upstream_client_id=settings['INVOICE_MCP_GOOGLE_CLIENT_ID'],
        upstream_client_secret=settings['INVOICE_MCP_GOOGLE_CLIENT_SECRET'],
        token_verifier=OwnerGoogleVerifier(required_scopes=scopes,
                                         audience=settings['INVOICE_MCP_GOOGLE_CLIENT_ID']),
        base_url=AUTH_BASE, issuer_url=AUTH_BASE,
        redirect_path='/auth/callback', client_storage=storage,
        jwt_signing_key=settings['INVOICE_MCP_SIGNING_KEY'],
        valid_scopes=scopes, require_authorization_consent=True,
        allowed_client_redirect_uris=['https://chatgpt.com/*', 'https://chat.openai.com/*',
                                      'http://localhost:*', 'http://127.0.0.1:*'],
        extra_authorize_params={'access_type': 'offline', 'prompt': 'consent'},
        enable_cimd=False,
    )
    return build_lookup_server(auth).http_app(
        path='/mcp', json_response=True, stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=['autocount-mobile-quick-invoice.vercel.app'],
        allowed_origins=[PUBLIC_BASE, 'https://chatgpt.com', 'https://chat.openai.com'],
    )
