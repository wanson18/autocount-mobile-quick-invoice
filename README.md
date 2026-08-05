# AutoCount Mobile Quick Invoice

Mobile-first invoice-entry for **AutoCount Cloud Accounting** (Malaysia). A
narrow server-side API issues real AutoCount invoices for the private
**Custom GPT** workflow, with idempotent creation, ambiguous-write
reconciliation, and a historical-price preview. A standalone mobile PWA and
iPhone Share Sheet follow after this launch slice works end to end.

Current launch priority: **Sdn Bhd-only private Custom GPT workflow** — issue
via the documented Cloud Accounting Integration API, keep
`saveApprove: true` / `submitEInvoice: false`, and make duplicate protection
mandatory. MyInvois submission remains a separate explicit workflow.

## Status

- **Implemented:** Tasks 1–9, 11 + historical price lookup — company
  isolation, AutoCount client + payload mapping, idempotent invoice service,
  ambiguous-write reconciliation, master-data search, official-PDF spike
  (fail-closed), price history, and the REST API with the Custom GPT Action
  schema. **334 tests pass.** Plus a mobile quick-invoice web page and
  Postgres-backed idempotency storage for serverless deployment (Vercel).
- **Pending:** Task 10 (e-Invoice technical spike), HTTPS deployment +
  Action registration, then Tasks 13–15 (Share Sheet, security, E2E).
- **Blocked:** official PDF sharing — AutoCount documents no PDF/print
  mechanism; the endpoint fails closed until one exists (see
  [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md)). The mobile
  page relies on opening AutoCount directly to share the PDF instead.

## Architecture

Hexagonal ports-and-adapters, Python 3.11+, FastAPI, Pydantic v2, httpx,
SQLite or Postgres for idempotency/audit metadata:

```text
app/
  main.py            FastAPI app, structured error handlers, GPT Action schema
  config.py          server-side company → account-book mapping (secrets only)
  dependencies.py    lazy singleton wiring from environment
  models/            invoice, master-data, company models
  autocount/         client (Key-ID/API-Key auth), adapter, payload mapping,
                     sanitised error hierarchy
  services/          invoice service (idempotency + reconciliation),
                     price-history lookup
  repositories/      SQLite and Postgres idempotency request repositories
  api/               companies, customers, products, invoices routers
  static/            mobile quick-invoice single-page web app
tests/unit/          contract and unit tests (no live network)
docs/autocount/      AutoCount research notes (e.g. pdf-spike)
```

Design rules:

- **The account-book ID is a server secret.** The client supplies only a
  `CompanyKey` (`enterprise` | `sdn_bhd`); the server resolves the account
  book and never accepts one from a request.
- **No duplicate invoices, ever.** Every issue carries an idempotency key;
  the same key + payload replays the original result without a second
  create. A timed-out (ambiguous) write is reconciled against the invoice
  listing before anything else — one exact line match completes it, zero
  fails it, several require manual reconciliation.
- **Fail closed.** Undocumented AutoCount features (e.g. PDF) raise
  `501 unsupported`; upstream errors are sanitised at the client boundary so
  no credential, account-book ID, or taxpayer data can leak.
- **Exact money.** All prices serialise as exact decimal strings, never
  binary floats.
- **No silent e-Invoice.** `submitEInvoice` stays `false`; the issue result
  reports `einvoice.status = not_requested`.

## HTTP API

| Method | Path | Operation ID | Purpose |
|---|---|---|---|
| GET | `/api/companies` | `listCompanies` | Company keys + display names (no account-book IDs) |
| GET | `/api/{company}/customers?q=` | `searchCustomers` | Customer search |
| GET | `/api/{company}/customers/{id}/addresses` | `listCustomerAddresses` | Delivery addresses |
| GET | `/api/{company}/products?q=` | `searchProducts` | Product search, `default_price` as string |
| POST | `/api/invoices/preview` | `previewInvoicePrices` | Latest prior price per item with source invoice/date |
| POST | `/api/invoices` | `issueInvoice` | Idempotent issue; `201` on create; marked consequential for GPT |
| GET | `/api/{company}/invoices/{id}/pdf` | `getInvoicePdf` | Official PDF — currently `501` (no documented mechanism) |
| GET | `/openapi.json` | — | Custom GPT Action schema |
| GET | `/` | — | Mobile quick-invoice web page |

Errors are structured JSON with an `error` code, safe `message`, and field
paths on validation failures: `400 invalid_invoice`, `409` idempotency
conflicts / pending / reconciliation-required, `422 validation_error`,
`501 unsupported`, `502` AutoCount rejections (with upstream status) and
ambiguous writes (`retryable: true`), `500` never leaks internals.

## Setup

```bash
# Python >= 3.11, then:
uv venv .venv
uv pip install -e ".[dev]"        # fastapi, uvicorn, pydantic, httpx, psycopg, pytest

# Run the test suite (no live network required)
.venv/Scripts/python.exe -m pytest tests/
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AUTOCOUNT_API_KEY_ID` | yes | AutoCount Cloud Integration API Key ID |
| `AUTOCOUNT_API_KEY` | yes | AutoCount Cloud Integration API Key |
| `AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE` | yes | Enterprise account-book ID |
| `AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD` | yes | Sdn Bhd account-book ID |
| `INVOICE_REQUESTS_DB` | no | SQLite idempotency DB path (default `data/invoice_requests.db`) — used only when `POSTGRES_URL`/`DATABASE_URL` is unset |
| `POSTGRES_URL` or `DATABASE_URL` | no (required on Vercel) | Postgres connection string; when set, idempotency storage switches from SQLite to Postgres. Required on serverless hosts (Vercel) where the local filesystem does not persist between invocations |

### Run the server

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
# Mobile quick-invoice page: http://localhost:8000/
# OpenAPI / GPT Action schema: http://localhost:8000/openapi.json
```

The deployment must be HTTPS with a valid certificate before it can be
registered as a Custom GPT Action; the Action pins the company to
`sdn_bhd`.

## Mobile quick-invoice page

`app/static/index.html` is a single-page mobile web app served from `/` —
company → customer/address → items (qty, price prefilled from price history)
→ review → issue. It calls the same REST API as the Custom GPT Action. There
is no PDF/share step: open AutoCount directly to share the official invoice
PDF after issuing.

Each issue attempt generates one `crypto.randomUUID()` idempotency key before
the request and reuses it on retry, so a flaky connection or a second tap
after a timeout can never create a duplicate invoice — the same guarantee the
GPT workflow relies on.

## Deploying to Vercel

The app deploys as a single Vercel Python Function (FastAPI auto-detected at
`app/main.py`); the mobile page is served from the same function via
`StaticFiles`, so there is one project, one origin, no CORS configuration.

```bash
npx vercel link
npx vercel env add AUTOCOUNT_API_KEY_ID production
npx vercel env add AUTOCOUNT_API_KEY production
npx vercel env add AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE production
npx vercel env add AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD production
npx vercel env add POSTGRES_URL production   # required — see below
npx vercel --prod
```

**Idempotency storage on Vercel.** Vercel Functions have an ephemeral
filesystem, so the default SQLite idempotency DB does not persist between
requests there — duplicate-invoice protection would silently stop working.
Set `POSTGRES_URL` (Vercel Postgres, or any managed Postgres) before going to
production; `app/dependencies.py` then uses
`app/repositories/postgres_request_repository.py` instead of the SQLite
repository. The schema is created automatically on first connection. To
verify the Postgres path locally before deploying, point `POSTGRES_TEST_DSN`
at a scratch database and run `pytest tests/unit/test_postgres_request_repository.py`.

## Documentation

- [`autocount_mobile_invoice_v1_spec.md`](autocount_mobile_invoice_v1_spec.md) — requirements specification
- [`autocount_mobile_invoice_v1_plan.md`](autocount_mobile_invoice_v1_plan.md) — implementation plan (Tasks 1–15)
- [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md) — official-PDF spike findings
