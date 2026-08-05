# Manual Test Checklist — Private Custom GPT Invoice Workflow

Live acceptance for the Sdn Bhd-only launch slice, before deploying the GPT
Action. Work through the sequence top to bottom on a **non-production
AutoCount account book** (plan Task 15). Every `issueInvoice` creates a
**real, approved invoice** (`saveApprove: true`) — there is no delete in the
flow; void test invoices in AutoCount afterwards.

The automated suite (334 tests) covers the error paths; this checklist proves
the same behaviour against the real AutoCount Cloud API.

## Prerequisites

1. Real AutoCount Cloud Integration API credentials and both account-book IDs.
2. A non-production account book for `sdn_bhd` (and `enterprise`) to test
   against.
3. Python 3.11+, `uv pip install -e ".[dev]"` done, server code current.

## 1. Start the server

```powershell
$env:AUTOCOUNT_API_KEY_ID = "..."
$env:AUTOCOUNT_API_KEY = "..."
$env:AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE = "..."
$env:AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD = "..."
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Sanity: `/openapi.json` loads and shows all 7 operation IDs
(`listCompanies`, `searchCustomers`, `listCustomerAddresses`,
`searchProducts`, `previewInvoicePrices`, `issueInvoice`, `getInvoicePdf`).

## 2. Company and master data

| Check | Command | Expected |
|---|---|---|
| Companies listed, no account-book IDs | `curl -s localhost:8000/api/companies` | 200, two entries `enterprise` + `sdn_bhd`; response body must not contain either account-book ID |
| Unknown company rejected | `curl -s localhost:8000/api/nonsense/customers` | 422 with `loc: ["path", "company"]` |
| Customer search | `curl -s "localhost:8000/api/sdn_bhd/customers?q=<known name>"` | 200, matching customers with id/code/name |
| Customer search, no match | `curl -s "localhost:8000/api/sdn_bhd/customers?q=zzz-no-such"` | 200, `"data": []` |
| Addresses | `curl -s "localhost:8000/api/sdn_bhd/customers/<id>/addresses"` | 200, delivery addresses with id/label/address_text |
| Product search | `curl -s "localhost:8000/api/sdn_bhd/products?q=<known item>"` | 200; `default_price` is a **string**, e.g. `"30.00"`, never a float |

## 3. Confirmation preview

| Check | Command | Expected |
|---|---|---|
| Price history | `curl -s -X POST localhost:8000/api/invoices/preview -H "Content-Type: application/json" -d '{"company":"sdn_bhd","customer_id":"<id>","item_ids":["<item>","<never-sold-item>"]}'` | 200; each item has `latest_unit_price` + `source_invoice_number`/`source_invoice_date`, or all-null for the never-sold item |
| Price plausibility | compare against the invoice listing in AutoCount | most recent non-cancelled price, within the last 90 days |
| Empty item list | `"item_ids": []` | 422 with `loc: ["body", "item_ids"]` |

## 4. Issue an invoice

Use a fixed `idempotency_key` (any stable string) — required, duplicate
protection is mandatory.

| Check | Command | Expected |
|---|---|---|
| Issue | `curl -s -X POST localhost:8000/api/invoices -H "Content-Type: application/json" -d '{...draft with idempotency_key...}'` | 201; `invoice_id`, `invoice_number`, `price_overrides` (original vs issued price), `einvoice.status = "not_requested"` |
| Exact prices | inspect the created invoice in AutoCount | qty and unit price match the payload **exactly** (e.g. `19.995` is not rounded to `20`) |
| Verified in AutoCount | open the invoice in the account book | exists, status approved/saved (`saveApprove`), correct debtor, date, lines |
| Replay same key + payload | repeat the exact same POST | 201, **same** `invoice_number`; AutoCount shows **one** invoice, no duplicate |
| Same key, changed payload | repeat with a different quantity | 409 `idempotency_conflict` |
| Unknown customer | draft with a customer not in the book | 400 `invalid_invoice` |
| Missing fields | `{"company":"sdn_bhd"}` | 422 with `loc` field paths |

## 5. Fail-closed behaviours

| Check | Command | Expected |
|---|---|---|
| PDF | `curl -s localhost:8000/api/sdn_bhd/invoices/<id>/pdf` | 501 `unsupported`, message names the missing mechanism |
| Bad credentials (temporarily break `AUTOCOUNT_API_KEY` and restart) | any master-data call | 502 `autocount_rejected`; response must not contain the key, key ID, or account-book IDs |

## 6. Reconciliation (optional, network-simulated)

The ambiguous-write path is covered by automated tests; to observe it live,
make the write time out by killing the network (or pointing
`AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD` at an unreachable value) during an
issue. Expect a `502 autocount_ambiguous_write` with `retryable: true` on the
first attempt; the next attempt with the **same** key either completes the
invoice (one match in the listing) or returns a reconciliation message — it
never creates a second invoice.

## 7. After local acceptance — deploy the GPT Action

1. Host the API on HTTPS with a valid certificate (required by Custom GPT
   Actions) and set the four env vars in the hosting environment.
2. Register `https://<host>/openapi.json` as a new Custom GPT Action; the
   schema already carries operation IDs and the
   `x-openai-isConsequential` marker on `issueInvoice`.
3. In the GPT configuration, pin the action's company to `sdn_bhd` — never
   let the model ask the user for a company key.
4. From the ChatGPT mobile app, run the happy path: pick customer → pick
   items → confirm the previewed prices → approve the consequential
   confirmation → verify the invoice in AutoCount.
5. Re-run the issue with the same details and confirm no duplicate invoice.

## Cleanup

- Void or delete every test invoice created above in AutoCount.
- Reset idempotency state between test rounds: delete
  `data/invoice_requests.db` while the server is stopped (or point
  `INVOICE_REQUESTS_DB` at a fresh path).
