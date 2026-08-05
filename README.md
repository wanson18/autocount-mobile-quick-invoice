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
  schema. **334 tests pass.**
- **Pending:** Task 10 (e-Invoice technical spike), HTTPS deployment +
  Action registration, then Tasks 12–15 (PWA, Share Sheet, security, E2E).
- **Blocked:** official PDF sharing — AutoCount documents no PDF/print
  mechanism; the endpoint fails closed until one exists (see
  [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md)).

## Architecture

Hexagonal ports-and-adapters, Python 3.11, FastAPI, Pydantic v2, httpx,
SQLite for idempotency/audit metadata:

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
  repositories/      SQLite idempotency request repository
  api/               companies, customers, products, invoices routers
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

Errors are structured JSON with an `error` code, safe `message`, and field
paths on validation failures: `400 invalid_invoice`, `409` idempotency
conflicts / pending / reconciliation-required, `422 validation_error`,
`501 unsupported`, `502` AutoCount rejections (with upstream status) and
ambiguous writes (`retryable: true`), `500` never leaks internals.

## Setup

```bash
# Python >= 3.11, then:
uv venv .venv
uv pip install -e ".[dev]"        # fastapi, uvicorn, pydantic, httpx, pytest

# Run the test suite (334 tests, no live network required)
.venv/Scripts/python.exe -m pytest tests/
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AUTOCOUNT_API_KEY_ID` | yes | AutoCount Cloud Integration API Key ID |
| `AUTOCOUNT_API_KEY` | yes | AutoCount Cloud Integration API Key |
| `AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE` | yes | Enterprise account-book ID |
| `AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD` | yes | Sdn Bhd account-book ID |
| `INVOICE_REQUESTS_DB` | no | Idempotency DB path (default `data/invoice_requests.db`) |

### Run the server

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
# OpenAPI / GPT Action schema: http://localhost:8000/openapi.json
```

The deployment must be HTTPS with a valid certificate before it can be
registered as a Custom GPT Action; the Action pins the company to
`sdn_bhd`.

## Documentation

- [`autocount_mobile_invoice_v1_spec.md`](autocount_mobile_invoice_v1_spec.md) — requirements specification
- [`autocount_mobile_invoice_v1_plan.md`](autocount_mobile_invoice_v1_plan.md) — implementation plan (Tasks 1–15)
- [`docs/autocount/pdf-spike.md`](docs/autocount/pdf-spike.md) — official-PDF spike findings
