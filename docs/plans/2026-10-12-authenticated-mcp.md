# Authenticated Invoice Generation lookup connection

Approved scope: extend the existing FastAPI/Vercel service and existing private
plugin without replacing their identities or altering the production REST API.

Use FastMCP's Google OAuth proxy with PKCE, consent and a verified-email allowlist
restricted to wanson18@gmail.com. Store OAuth state durably in the existing
Postgres database, encrypted with a dedicated server-side key. Missing Google
configuration must deny access, never start an anonymous MCP server.

Expose read-only company/customer/address/product/price tools using the existing
adapters. Require an explicit company and that company's own credential pair;
reject shared credentials and duplicate book routing. Do not expose invoice
issue, edit or e-invoice submission tools in this customer-test connection.

Verify authentication denial, email restrictions, routing isolation, no write
tools, real MCP discovery/calls and the entire existing test suite. Deploy the
reviewed commit through the current Vercel project. Overlay mcp.json and the
version bump onto the current plugin; preserve its original instructions/assets.
Read back the saved release. Test Seasons separately and clearly distinguish
REST verification from authenticated MCP/host verification if Google setup
prevents the latter. Google app credentials must be configured in Vercel by the
owner, never pasted into chat or packaged with the plugin.
