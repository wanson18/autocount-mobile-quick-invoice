# AutoCount Mobile Quick Invoice V1 — Implementation Plan

**Goal:** Build a mobile-first invoice-entry application that creates real invoices in either AutoCount Cloud account book and shares the official invoice PDF through the iPhone Share Sheet.

**Recommended stack:** Python 3.12+, FastAPI, Pydantic v2, httpx, SQLite for idempotency/audit metadata, pytest, and a mobile PWA front end.

## Current Launch Priority — Private Custom GPT

Deliver the Sdn Bhd-only private GPT workflow before the standalone PWA:

1. Complete invoice mapping, service, reconciliation, and read-back.
2. Add latest exact customer/item historical-price lookup from prior
   non-cancelled AutoCount invoices, returning the source invoice/date for the
   confirmation preview.
3. Expose narrow preview and confirmed-issue HTTPS endpoints with a Custom GPT
   Action OpenAPI schema. Keep the Sdn Bhd account book fixed server-side.
4. Retrieve the official AutoCount PDF and return a short-lived download URL.
5. Keep `saveApprove: true`, `submitEInvoice: false`, and duplicate protection
   mandatory. MyInvois remains a separate explicit workflow.

The two-company selector, standalone PWA, and iPhone Share Sheet follow after
this launch slice works end to end in the ChatGPT mobile app.

## Proposed Structure

```text
app/
  main.py
  config.py
  models/
    invoice.py
    master_data.py
  autocount/
    client.py
    adapter.py
    mapping.py
    errors.py
  services/
    invoice_service.py
    idempotency.py
    audit.py
  repositories/
    request_repository.py
  api/
    companies.py
    customers.py
    products.py
    invoices.py
  static/
    index.html
    app.js
    styles.css
tests/
  unit/
  integration/
  ui/
docs/
  autocount/
  testing/
```

## Task 1 — Configuration and Company Isolation

Create server-side mappings for:

- `enterprise` → Wanson Enterprise AutoCount account book
- `sdn_bhd` → Wanson Enterprise (M) Sdn Bhd account book

Acceptance checks:

- Account-book IDs are distinct.
- Browser cannot supply arbitrary account-book IDs.
- All downstream customer, item, address, invoice, PDF, and e-Invoice calls use the selected server-side company configuration.

Commit checkpoint:

```bash
git commit -m "feat: add AutoCount company isolation config"
```

## Task 2 — Domain Models

Implement strict Pydantic models:

```python
class InvoiceLineInput(BaseModel):
    item_id: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    original_unit_price: Decimal = Field(ge=0)

class InvoiceDraftInput(BaseModel):
    company: CompanyKey
    invoice_date: date
    customer_id: str
    delivery_address_id: str
    lines: list[InvoiceLineInput] = Field(min_length=1)
    submit_einvoice: bool = False
    idempotency_key: str
```

Tests must reject zero quantity, negative price, empty lines, invalid company, and extra unexpected fields.

## Task 3 — AutoCount HTTP Client

Create an asynchronous `AutoCountClient` using `httpx`.

Responsibilities:

- Add authentication server-side.
- Add the selected account book to every call.
- Apply timeouts.
- Normalise AutoCount rejections.
- Distinguish an ordinary rejection from an ambiguous timeout during a write.
- Redact credentials from exceptions and logs.

Commit checkpoint:

```bash
git commit -m "feat: add AutoCount API client boundary"
```

## Task 4 — Master-Data Adapter

Implement:

```python
search_customers(company, query)
get_delivery_addresses(company, customer_id)
search_items(company, query)
get_item(company, item_id)
```

Normalised outputs:

```python
CustomerSummary(id, code, name)
DeliveryAddress(id, label, address_text)
ProductSummary(id, code, name, default_price)
```

Tests must prove that Enterprise and Sdn Bhd data cannot cross over, and that an address belongs to the selected customer.

## Task 5 — Idempotency Repository

Create a SQLite table:

```sql
CREATE TABLE invoice_requests (
  idempotency_key TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  autocount_invoice_id TEXT,
  autocount_invoice_number TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Rules:

- Same key plus same request returns the stored result.
- Same key plus different request is rejected.
- Pending and ambiguous requests are reconciled before replay.

## Task 6 — Invoice Payload Mapping

Implement a mapper from the mobile domain model to the documented AutoCount invoice API.

Include only:

- invoice date,
- customer,
- delivery address,
- item,
- quantity,
- issued unit price,
- supported save/approve action.

Do not include PO/reference, salesperson, remarks, free-text lines, or a client-generated invoice number.

Tests must verify the overridden price is sent as the issued accounting price while the original price remains audit metadata only.

## Task 7 — Invoice Service

Implement:

```python
InvoiceService.issue(draft) -> InvoiceCreateResult
```

Required sequence:

```text
validate draft
→ canonical request hash
→ begin idempotency
→ return stored result when complete
→ validate customer/address/item ownership
→ create AutoCount invoice
→ store AutoCount invoice ID/number
→ record price overrides
→ optionally request e-Invoice processing
→ return result
```

The normal invoice result must be preserved when the e-Invoice step fails.

## Task 8 — Ambiguous Write Reconciliation

On write timeout:

- Do not automatically repeat the create call.
- Query AutoCount for a likely matching invoice.
- Prefer a supported custom reference/idempotency field if AutoCount exposes one.
- Otherwise use a conservative signature: account book, customer, date, exact lines, total, and narrow creation-time window.
- If multiple candidates exist, require manual reconciliation instead of creating another invoice.

Test the case where AutoCount created the invoice but the response timed out. A second tap must return the original invoice and create no duplicate.

## Task 9 — Official PDF Technical Spike

Document the exact supported method for retrieving AutoCount’s official PDF:

- documented PDF API,
- print/export API,
- supported authenticated download URL,
- or other supported mechanism.

Adapter contract:

```python
async def get_invoice_pdf(company, invoice_id) -> bytes:
    ...
```

Test that returned bytes start with `%PDF`.

Do not use production HTML scraping when no supported mechanism exists. Keep sharing disabled until the spike succeeds.

## Task 10 — e-Invoice Technical Spike

Verify AutoCount support for:

- triggering e-Invoice submission,
- checking submission status,
- retrieving validation failure details.

Normalised statuses:

```text
not_requested
pending
submitted
action_required
unsupported
```

Do not add direct MyInvois integration unless AutoCount documentation explicitly requires that architecture.

## Task 11 — REST API

Implement:

```text
GET  /api/companies
GET  /api/{company}/customers?q=
GET  /api/{company}/customers/{customer_id}/addresses
GET  /api/{company}/products?q=
POST /api/invoices
GET  /api/{company}/invoices/{invoice_id}/pdf
```

Return structured validation errors with a field path. Test invalid company, cross-company data, empty lines, duplicate keys, successful issue, and PDF response type.

## Task 12 — Mobile PWA

Build the screens:

1. Company selection
2. Customer and address selection
3. Item-line editor
4. Final review
5. Issue result

Line editor fields:

```text
Item | Quantity | Unit Price | Line Total | Remove
```

The selected company remains visible throughout.

Generate `crypto.randomUUID()` before issue, disable the button immediately, and reuse the same key for retrying the same request after a connectivity interruption.

## Task 13 — iPhone Share Sheet

Use the Web Share API:

```javascript
const file = new File([pdfBlob], `${invoiceNumber}.pdf`, {
  type: "application/pdf"
});

if (navigator.canShare?.({ files: [file] })) {
  await navigator.share({ files: [file], title: invoiceNumber });
} else {
  // Open or download the PDF.
}
```

Do not encode or select a WhatsApp recipient. The user selects WhatsApp and the contact manually.

Manual iPhone acceptance tests:

- Share Sheet opens in Safari.
- PDF is attached.
- WhatsApp is available when installed.
- Cancelling share does not affect invoice state.
- Sharing again does not create another invoice.

## Task 14 — Security

Before production:

- Add user authentication.
- Enforce HTTPS.
- Keep AutoCount credentials server-side.
- Redact credentials and taxpayer data from logs.
- Ignore or reject client-supplied account-book IDs.
- Recalculate and validate totals server-side.
- Confirm AutoCount secrets never appear in HTML, JavaScript, OpenAPI output, or API responses.

## Task 15 — End-to-End Acceptance

Run the full automated test suite and perform non-production tests for both companies.

Verify for each company:

- correct account book,
- correct customer and address,
- correct item,
- correct quantity,
- correct default and overridden price,
- exactly one AutoCount invoice,
- AutoCount-generated invoice number,
- proper e-Invoice status separation,
- official PDF retrieval,
- iPhone Share Sheet to WhatsApp.

Record test invoice numbers and clean-up or void actions.

## Implementation Order

1. Configuration and domain models
2. AutoCount client and master data
3. Idempotency
4. Invoice mapping and service
5. Reconciliation
6. REST API
7. Mobile PWA
8. PDF spike
9. e-Invoice spike
10. iPhone sharing
11. Security and acceptance testing

## Completion Gate

Do not declare production-ready until:

- company isolation tests pass,
- duplicate-creation tests pass,
- ambiguous-write reconciliation passes,
- the supported AutoCount PDF mechanism is verified,
- e-Invoice behaviour is verified or clearly marked unsupported,
- and the complete workflow has been tested on an actual iPhone.
