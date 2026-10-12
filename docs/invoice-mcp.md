# Invoice Generation MCP connection

Endpoint: `https://autocount-mobile-quick-invoice.vercel.app/invoice-mcp/mcp`

This is an additional read-only connection. It does not replace `/api/*`, the
mobile website or the existing Invoice Generation plugin. Available tools:
`listCompanies`, `searchCustomers`, `listCustomerAddresses`, `searchProducts`,
`previewInvoicePrices`. It cannot issue/edit invoices or submit e-invoices.
Historical price lookup is advisory, not a complete invoice preview.

## Google sign-in setup (owner action)

Create a Google OAuth **Web application** client in the intended Google Cloud
project. Register this exact authorized redirect URI:

`https://autocount-mobile-quick-invoice.vercel.app/invoice-mcp/auth/callback`

Configure these Vercel environment variables for the existing project:

- `INVOICE_MCP_GOOGLE_CLIENT_ID`: the client's ID.
- `INVOICE_MCP_GOOGLE_CLIENT_SECRET`: its secret, saved as sensitive.
- `INVOICE_MCP_ALLOWED_EMAILS`: `wanson18@gmail.com`.
- `INVOICE_MCP_SIGNING_KEY`: dedicated random secret, at least 32 characters;
  save as sensitive. Stable across deployments; rotation invalidates sessions.

Use the existing `POSTGRES_URL`/`DATABASE_URL`; no accounting database changes
are required. OAuth state is encrypted in a separate `invoice_mcp_oauth` table
created lazily on first sign-in. The key-value library marks this storage backend
as unstable; it is pinned, and upgrades require a regression check.

Redeploy after setting credentials. Connect the updated private plugin and sign
in as the allowed Google user. Credentials/tokens must never appear in plugin
files, URLs, browser page source, chat, or Git. Google grants only sign-in/email
scopes, not Gmail/Drive access. Consent and PKCE remain enabled. Dynamic client
redirects are restricted to ChatGPT and local MCP desktop callbacks.

If setup is incomplete, `/invoice-mcp/mcp` returns 401 and auth routes return
503. The existing mobile API remains independent. Do not describe this as a
successful authenticated connection until an actual owner sign-in and lookup
have been completed through the installed plugin.

## Company isolation and approval

Every lookup requires `enterprise` or `sdn_bhd`. Account books are resolved
server-side; arbitrary account-book IDs and extra arguments are rejected.
MCP requires the selected company's own credential pair and never falls back
to shared credentials. Existing checks reject duplicate account-book routing.

Keep the plugin's full preview and explicit post-preview approval rules. The
read-only MCP exposes no invoice issuance tool; do not route around that absence
to issue via raw HTTP. E-invoice auto submission remains unavailable here.

## Verification

Run `python -m pytest tests/`. The new tests exercise real MCP transport with
test-only auth and synthetic upstream records, deny anonymous access, validate
metadata/PKCE, reject hostile origins, test verified owner email restrictions,
and demonstrate separate company routing. They do not prove live Google sign-in.
Live Seasons REST reads should be labelled REST verification while sign-in is
pending, not represented as authenticated plugin calls.
