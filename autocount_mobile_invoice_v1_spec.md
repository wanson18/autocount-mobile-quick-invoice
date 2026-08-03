# AutoCount Mobile Quick Invoice V1 — Design Specification

**Date:** 2026-08-01  
**Status:** Approved V1 design  
**Primary users:** Wanson Enterprise and Wanson Enterprise (M) Sdn Bhd operations

## Current Launch Slice — Private ChatGPT Invoice GPT

The first usable release is a private Custom GPT opened from the ChatGPT mobile
app, not the standalone PWA. The backend remains a small HTTPS API, exposed to
the GPT through a narrow Action schema.

- Company is fixed server-side to Wanson Enterprise (M) Sdn Bhd for this slice.
- The user sends an order photo and clarifies the customer/items in chat.
- For each exact customer/item pair, the backend proposes the latest price from
  a prior non-cancelled AutoCount invoice and identifies the source invoice and
  date. The user may change that price before confirming.
- The GPT must show a complete preview and obtain explicit confirmation before
  calling the issue action.
- A confirmed request creates and approves the normal AutoCount invoice with
  `saveApprove: true`. MyInvois/e-Invoice flags remain off and require a
  separate explicit workflow.
- The backend verifies the created invoice before returning its official PDF,
  preferably through a short-lived download URL suitable for ChatGPT mobile.
- AutoCount credentials stay on the backend. The GPT Action stores only a
  separate gateway credential.

The existing two-company PWA remains a later delivery target; it is deferred,
not removed.

## 1. Objective

Build a fast, mobile-first invoice interface for iPhone that creates real invoices directly in AutoCount Cloud.

AutoCount Cloud remains the sole system of record for invoice numbering, accounting treatment, customers, delivery addresses, items, taxes, approval state, invoice PDFs, and e-Invoice processing.

Primary workflow:

**Choose Company → Select Customer → Select Delivery Address → Add Items → Enter Quantity → Review/Edit Price → Review Invoice → Optional e-Invoice flag → Confirm & Issue → Share AutoCount PDF through iOS Share Sheet → WhatsApp**

## 2. V1 Scope

### Included

- Support both Wanson Enterprise and Wanson Enterprise (M) Sdn Bhd.
- Explicit company selection before invoice entry.
- Existing AutoCount customers only.
- Existing delivery addresses only.
- Existing AutoCount items only.
- Invoice date defaults to the current date in Asia/Kuala_Lumpur.
- Quantity entry for each invoice line.
- AutoCount unit price prefilled and editable before issue.
- Final review screen.
- Optional `Submit e-Invoice` checkbox.
- Normal invoice creation succeeds independently of e-Invoice submission.
- Retrieval of the official AutoCount invoice PDF where supported.
- iOS Share Sheet for manual sharing to WhatsApp.
- Duplicate-submission protection using idempotency.
- Clear error handling and retry behaviour.

### Excluded

- PO/reference field.
- Salesperson field.
- Remarks field.
- New customer, address, or item creation.
- Free-text invoice lines.
- Automatic WhatsApp recipient matching or sending.
- AR, stock, approval, ECOSS, Google Sheets, or Airtable dashboards.
- Repeat-last-invoice and favourite-item shortcuts.
- Custom invoice numbering.
- Independent tax calculation.
- Direct MyInvois integration unless AutoCount explicitly requires and supports it.
- Any separate invoice database acting as an accounting ledger.

## 3. Core Principle

The application is a thin transaction-entry front end to AutoCount Cloud. It may store short-lived UI state, request logs, idempotency records, and cached master data, but it must never become authoritative for accounting data.

Every successful issue action must create a real AutoCount invoice.

## 4. Company Isolation

The first screen shows two large choices:

- Wanson Enterprise
- Wanson Enterprise (M) Sdn Bhd

The selected company controls the AutoCount account book used for customer search, address retrieval, item search, pricing, invoice creation, PDF retrieval, and e-Invoice status.

The selected company must remain visible during entry and review. Customer, item, address, and invoice identifiers must never be mixed between account books.

## 5. Invoice Entry

The form contains only:

1. Invoice date
2. Customer
3. Delivery address
4. Invoice lines: item, quantity, unit price, line total
5. Invoice total

### Invoice date

- Defaults to the current local date.
- Backdating is permitted only when AutoCount and the user’s permissions allow it.
- Future dates are rejected in V1.

### Customer and address

- Search existing customers in the selected account book.
- Display code and name to distinguish similar customers.
- Load only delivery addresses belonging to the selected customer.
- Auto-select the default address where AutoCount supplies one.
- No address creation or editing in V1.

### Items and price

- Search existing items only.
- Quantity must be greater than zero.
- Unit price comes from AutoCount’s applicable price data.
- Unit price may be overridden before issue.
- Price overrides are recorded in a non-authoritative audit log with original price, issued price, time, and actor/session.

## 6. Review and Issue

The review screen displays:

- company,
- invoice date,
- customer,
- delivery address,
- all items,
- quantities,
- unit prices,
- line totals,
- grand total,
- `Submit e-Invoice` checkbox,
- `Confirm & Issue` button.

The issue button is disabled immediately after the first valid tap. The browser sends an idempotency key so retries cannot create duplicate invoices.

## 7. AutoCount Invoice Creation

The backend maps the reviewed draft into AutoCount’s supported invoice-creation API.

Rules:

- AutoCount allocates the invoice number.
- AutoCount remains authoritative for tax and accounting treatment.
- The application stores only correlation and audit metadata.
- Success is shown only after AutoCount confirms invoice creation.
- Ambiguous timeouts must be reconciled against AutoCount before retrying.

## 8. e-Invoice Behaviour

### Unchecked

Create the normal AutoCount invoice without requesting e-Invoice processing.

### Checked

Create the normal invoice first, then invoke AutoCount’s supported e-Invoice workflow if one is documented and available.

If AutoCount offers no supported trigger/status method, the application must show the feature as unavailable or requiring processing inside AutoCount. It must not imitate submission or silently build a separate MyInvois integration.

### Failure separation

An e-Invoice failure must not roll back or hide a successfully created invoice.

Example:

- Invoice: Issued
- e-Invoice: Action required
- Reason: Missing or invalid taxpayer data

## 9. Official Invoice PDF

The preferred output is the PDF generated by AutoCount through a documented API, print/export endpoint, or supported authenticated download mechanism.

The application must not reproduce the official invoice layout independently unless AutoCount offers no supported PDF method and a separate fallback is explicitly approved.

PDF retrieval is a mandatory technical spike before production rollout.

## 10. WhatsApp Sharing

After the PDF is available:

1. User taps `Share PDF`.
2. The PWA invokes the iOS Web Share API.
3. iOS Share Sheet opens.
4. User selects WhatsApp.
5. User selects the contact and sends manually.

Fallback: open or download the PDF when file sharing is unavailable.

No automatic recipient selection and no silent sending.

## 11. Architecture

### Front end

Mobile-first PWA responsible for company selection, master-data search, invoice lines, review, issue command, result display, and iOS file sharing.

### Backend

Small API service responsible for authentication, account-book isolation, validation, idempotency, AutoCount API calls, reconciliation, audit logging, PDF retrieval, and e-Invoice adapter logic.

### AutoCount adapter

All AutoCount-specific endpoints and field mappings are isolated behind an adapter so the UI and domain service do not depend directly on AutoCount response structures.

## 12. Security

- AutoCount credentials exist only server-side.
- HTTPS is mandatory.
- Users must authenticate.
- Backend authorisation validates the selected company.
- Account-book IDs are selected from server configuration, never trusted from the browser.
- Sensitive tokens and taxpayer data are redacted from logs.
- Client-calculated totals are revalidated server-side.

## 13. Duplicate Prevention

For each issue request:

1. Generate an idempotency key before submission.
2. Store a pending request record.
3. Submit to AutoCount.
4. Store the resulting AutoCount invoice ID and number.
5. Return the stored result for repeated requests using the same key and payload.
6. Reconcile ambiguous timeouts before allowing another create request.

Reuse of the same idempotency key with a different request payload must be rejected.

## 14. Error Handling

- Validation errors identify the exact field.
- AutoCount rejection preserves the form for correction and retry.
- Ambiguous network failures trigger reconciliation, not an immediate replay.
- e-Invoice failure is displayed separately from invoice success.
- PDF retrieval failure does not mark the invoice as failed; PDF retrieval can be retried.

## 15. Minimum Testing

- Company isolation.
- Customer and item search per company.
- Address ownership validation.
- AutoCount default-price retrieval.
- Price override submission and audit.
- Invoice total validation.
- Successful invoice creation.
- AutoCount rejection.
- Duplicate-tap/idempotency behaviour.
- Timeout reconciliation.
- e-Invoice success and failure separation.
- PDF retrieval success and failure.
- iOS sharing capability detection.
- Confirmation that no API secret appears in browser assets or responses.

Use a test or sandbox account book wherever possible.

## 16. Production Spikes

### AutoCount PDF spike

Verify the exact supported mechanism for retrieving the official invoice PDF after API creation. WhatsApp sharing remains blocked until confirmed.

### AutoCount e-Invoice spike

Verify whether AutoCount provides a supported API or integration method to trigger submission and retrieve status. Without such support, V1 creates the normal invoice and shows e-Invoice as unavailable/manual.

## 17. Success Criteria

V1 is complete when the user can:

- select either company,
- search the correct account book,
- select a valid customer and address,
- add existing items,
- enter quantity,
- accept or override AutoCount price,
- review the invoice,
- create exactly one real AutoCount invoice,
- receive the AutoCount invoice number,
- optionally request supported e-Invoice processing without blocking invoice creation,
- retrieve the official AutoCount PDF where supported,
- and share it manually through the iPhone Share Sheet to WhatsApp.
