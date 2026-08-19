# AutoCount Mobile Quick Invoice

Mobile-first invoice-entry for **AutoCount Cloud Accounting** (Malaysia). A
narrow server-side API issues real AutoCount invoices for the private
**Custom GPT** workflow, with idempotent creation, ambiguous-write
reconciliation, and a historical-price preview. A standalone mobile PWA and
iPhone Share Sheet follow after this launch slice works end to end.

Current launch priority: **Sdn Bhd-only private Custom GPT workflow** — issue
via the documented Cloud Accounting Integration API, keep
`saveApprove: true` / `submitEInvoice: false`, and make duplicate protection
mandatory. e-Invoice/MyInvois submission is out of scope for this app and is
handled manually by the user.

## Status

- **Live and verified.** The mobile quick-invoice page is deployed on Vercel
  and has successfully issued a real invoice in AutoCount Cloud Accounting
  for Wanson Enterprise (M) Sdn Bhd end to end (customer search → item
  search → review → issue), confirmed via live browser testing against
  production.
- **Implemented:** Tasks 1–9, 11 + historical price lookup — company
  isolation, AutoCount client + payload mapping, idempotent invoice service,
  ambiguous-write reconciliation, master-data search, official-PDF spike
  (fail-closed), price history, and the REST API with the Custom GPT Action
  schema. **352 tests pass.** Plus a mobile quick-invoice web page and
  Postgres-backed idempotency storage for serverless deployment (Vercel).
- **Out of scope:** Task 10 (e-Invoice technical spike) — dropped 2026-08-06.
  e-Invoice/MyInvois submission is handled manually by the user outside this
  app; `submitEInvoice` stays `false` and `einvoice.status` stays
  `not_requested`.
- **Pending:** Tasks 13–15 (Share Sheet, security, E2E).
- **Blocked:** official PDF sharing — AutoCount documents no PDF/print
  mechanism; the endpoint fails closed until one exists (see
  [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md)). The mobile
  page instead offers **Open Cloud Report**, a hidden server-side redirect
  (`GET /api/{company}/invoices/{doc_no}/cloud-report`, excluded from the
  Custom GPT schema) that opens the verified AutoCount Cloud report screen
  where Print / Export PDF / Share happen. The same handoff is available from
  both the post-issue result screen and the Recent Invoice detail screen. Its
  live fresh-tab stability and visual report/Print behavior are pending Task 4
  verification.

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
  no credential, account-book ID, or taxpayer data can leak. A malformed or
  unexpected AutoCount response (e.g. a create response missing its
  `location` header) raises `AutoCountDataError` rather than silently
  reporting success.
- **Exact money.** All prices serialise as exact decimal strings, never
  binary floats.
- **No silent e-Invoice.** `submitEInvoice` stays `false`; the issue result
  reports `einvoice.status = not_requested`.

### AutoCount API contract notes

A few parts of AutoCount's Cloud Accounting Integration API are
under-documented or easy to get wrong; these are captured in code comments
in `app/autocount/mapping.py` and `app/services/invoice_service.py`, and
summarised in [`agents.md`](agents.md) — read those before changing invoice
payload construction or response parsing. In short: `accNo` is a per-line
field (not on the invoice master), `creditTerm` must match an exact
`CreditTermKey` configured in AutoCount, and Create Invoice's real success
response is a bare `201` with only a `location` header — the invoice's
identity (`docKey`/`docNo`) must be recovered via a follow-up Get Invoice
call, not assumed from a JSON body.

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
| `AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE` | no (required for Cloud handoff) | Server-side HTTPS AutoCount Cloud report template with one `{doc_key}` placeholder; keep the account-book path out of source and browser assets |
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
is no PDF/share step inside the app. After issuing, the result screen offers
**Open Cloud Report** (a new-tab deep link to the verified AutoCount Cloud
report screen, opened with `noopener,noreferrer`). Print, Export PDF, and Share
are all performed in that Cloud report screen. The
deep link route is hidden from the Custom GPT schema and substitutes the
server-confirmed AutoCount `docKey` into a server-side URL template — the
account-book path and any credentials live only in the server environment.

Each issue attempt generates one `crypto.randomUUID()` idempotency key before
the request and reuses it on retry, so a flaky connection or a second tap
after a timeout can never create a duplicate invoice — the same guarantee the
GPT workflow relies on.

## Deploying to Vercel

The app deploys as a single Vercel Python Function (FastAPI auto-detected at
`app/main.py`); the mobile page is served from the same function via
`StaticFiles`, so there is one project, one origin, no CORS configuration.
The project is currently deployed at
`https://autocount-mobile-quick-invoice.vercel.app/` and auto-deploys to
Production on every push to `main`.

```bash
npx vercel link
npx vercel env add AUTOCOUNT_API_KEY_ID production
npx vercel env add AUTOCOUNT_API_KEY production
npx vercel env add AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE production
npx vercel env add AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD production
npx vercel env add AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE production
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

- [`agents.md`](agents.md) — working rules for this repo, plus a running list
  of AutoCount API gotchas discovered against the live account book
- [`autocount_mobile_invoice_v1_spec.md`](autocount_mobile_invoice_v1_spec.md) — requirements specification
- [`autocount_mobile_invoice_v1_plan.md`](autocount_mobile_invoice_v1_plan.md) — implementation plan (Tasks 1–15)
- [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md) — official-PDF spike findings
