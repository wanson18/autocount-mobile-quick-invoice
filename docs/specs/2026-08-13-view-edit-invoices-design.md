# Design: View and edit issued invoices

**Date:** 2026-08-13
**Status:** Approved; live spike passed, one correction applied
**Branch:** `claude/view-edit-invoices-crkk25`

> **Spike outcome (2026-08-13).** All three open questions were answered
> against the live Sdn Bhd account book — see
> [`docs/autocount/invoice-update-spike.md`](../autocount/invoice-update-spike.md).
> An approved invoice **does** accept `PUT` (204), and a shorter `details`
> array **does** delete the trailing rows, so full-state replace stands. One
> correction: the header is **not** preserved by omitting `master`. `master`
> is required and must carry the five mandatory fields echoed from the
> invoice being edited. The relevant sections below have been updated.

## Problem

The app is create-only. `POST /api/invoices` issues an invoice and the mobile
page's five-step wizard (company → customer → items → review → result) ends at
a success screen. There is no way to look back at what was issued, and no way
to correct a mistake without opening AutoCount directly.

The read plumbing already exists but is not reachable over HTTP:
`AutoCountMasterDataAdapter.search_invoices` pages `POST /invoice/listing` and
normalises rows into `InvoiceSummary`, used internally for price history and
ambiguous-write reconciliation only.

## Scope

**In scope**

- Browse recently issued invoices for the selected company.
- Open one invoice and see its header and lines.
- Edit an invoice's line set: add a line, remove a line, change an existing
  line's quantity or unit price.
- Mobile web page only.

**Out of scope** (explicitly decided, not deferred by omission)

- Editing header fields — invoice date, customer, delivery address.
- Voiding, cancelling, or deleting an invoice.
- Exposing any of this to the Custom GPT Action. The new endpoints are hidden
  from `/openapi.json`, which is the schema the GPT reads.
- Recording invoice edits in the idempotency/audit repository.
- Anything touching the create path, e-Invoice, or PDF.

## Decisions

### Approach: full-state replace

AutoCount's `PUT /{accountBookId}/invoice?docNo=` treats `details` as a
positional array. Omitted fields keep their previous values; sending fewer
rows than currently exist deletes the trailing rows; the documented way to
preserve a row in place is an empty object `{}` in its slot.

Every edit sends the **complete desired line array**, every surviving row
spelled out in full, in order. No `{}` placeholders. Removing line 2 of 3
sends `[line1, line3]`: position 1 is rewritten with line1, position 2 with
line3, position 3 falls off the end and is deleted. One code path covers add,
remove, reorder, and field edits.

Rejected alternatives:

- **Minimal diff with `{}` placeholders.** Matches the documented idiom and
  sends less data, but an off-by-one silently rewrites the wrong row, and a
  retry after a partial apply is not safe. These invoices are a handful of
  lines; the payload saving is worthless.
- **Void and reissue.** Lowest-risk code, reusing only the create path already
  proven live, but it burns a document number per correction, litters the
  book with voided invoices, and void is not the operation the user wants.

The decisive property of full-state replace: the request encodes absolute
desired state, so sending it twice converges on the same invoice instead of
compounding. That makes the ambiguous-write case decidable by re-reading, and
removes any need for idempotency-key machinery on the edit path.

### Editability boundary

An invoice is editable only when **both** hold:

- `is_cancelled` is false, and
- `doc_date` falls within the last 30 days (`EDIT_WINDOW_DAYS = 30`).

Older or cancelled invoices are view-only; correct those in AutoCount
directly. The mobile page hides the Edit button accordingly, and the `PUT`
endpoint re-checks the same rule server-side — a hidden button is not a
control.

### No idempotency keys on edit

The create path carries a mandatory idempotency key, a request repository, and
a reconciliation routine because a repeated create makes a second invoice. A
repeated full-state `PUT` does not: it re-asserts the same line array. The
edit path therefore adds no repository rows, no new tables, and no key
handling. Its two write hazards are handled directly (below).

## Architecture

### Reads — `app/autocount/adapter.py`

Two new methods, both reusing the existing `_listing` paging loop and
`_invoice_row` normalisation:

- `list_recent_invoices(company, *, date_from, date_to) -> list[InvoiceSummary]`
  — `POST /invoice/listing` filtered on the `date` (docDate) range, with no
  `debtorCode` filter. The existing `search_invoices` keeps its
  `debtorCode` + `createdDate` filter and is not modified; price history and
  reconciliation depend on its exact behaviour.
- `get_invoice(company, doc_no) -> InvoiceSummary` — `GET /invoice?docNo=`.
  Returns the same `master`/`details` view model as a listing row, so it
  normalises through `_invoice_row` unchanged. The returned `docNo` must match
  the requested one or the call fails closed with `AutoCountDataError`, the
  same pattern `get_item` and `get_delivery_addresses` already use.

`get_invoice` is also the read used by the stale-write guard and the
ambiguous-write reconciliation, so it must not be cached.

### Model change — `app/models/master_data.py`

`InvoiceLineSummary` gains a `description: str` field, populated from the
detail row's `description`. Without it the edit screen would need one
`get_item` call per line to show product names. Additive; `search_invoices`
callers (price history, reconciliation) match on `product_code`, `qty`, and
`unit_price` and are unaffected.

### Writes — `app/services/invoice_edit_service.py` (new)

A separate module rather than an addition to `invoice_service.py`. That file
is 497 lines and its entire documented contract is "create one idempotent
AutoCount invoice from a confirmed mobile draft"; editing keys on `docNo`
rather than an idempotency key, has different failure modes, and has no
e-Invoice concern. Expected size ~200 lines.

Responsibilities:

1. Resolve the company server-side (`get_company`), never from the request.
2. `get_invoice` the current state.
3. Enforce the editability boundary.
4. Enforce the stale-write guard.
5. Resolve every edited line's product through `master_data.get_item`, exactly
   as the create path does, so an item from another account book cannot be
   introduced.
6. Build the payload and `client.write(company, "PUT", "invoice", params={"docNo": ...}, json=payload)`.
7. On `AutoCountAmbiguousWriteError`, reconcile by re-reading.
8. Return the updated `InvoiceSummary`.

### Payload builder — `app/autocount/mapping.py`

`map_invoice_update_payload(lines, products, master) -> dict` alongside the
existing `map_invoice_payload`. It emits `{"master": {...}, "details": [...]}`:

- `master` carries exactly the five documented mandatory fields — `docDate`,
  `debtorCode`, `debtorName`, `creditTerm`, `salesLocation` — echoed verbatim
  from the invoice being edited. It **cannot** be omitted: the Invoice Input
  Model marks it required and the live API rejects a body without it
  (`400 The Master field is required.`). `docDate` comes back as a datetime
  (`2026-08-13T00:00:00`) and is echoed unchanged, never reformatted.
- Every **optional** header field — `deliverAddress`, `description`, remarks —
  is left unsent, and is preserved rather than blanked (confirmed live). That
  is how the header survives a line edit.
- Each row carries `productCode`, `description`, `qty`, `unitPrice`, and
  `accNo` (`DEFAULT_ACC_NO`), matching what the create path sends per line.
  `description` is the resolved product's `name` from `get_item`, exactly as
  `map_invoice_payload` does — not the description stored on the existing
  invoice line. An edited invoice therefore carries the same descriptions a
  freshly created one would.
- `qty` and `unitPrice` are `Decimal`, never `str` and never `float`; the
  client's `_encode_json_body` emits them as exact bare JSON numbers.
- `saveApprove` is not sent. Whether an update needs it is one of the open
  questions the live spike answers (below).

### API — `app/api/invoices.py`

Three endpoints, all declared `include_in_schema=False` so they never appear
in `/openapi.json` and therefore never reach the Custom GPT Action:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/{company}/invoices?days=30` | Recent invoices, newest first |
| GET | `/api/{company}/invoices/{doc_no}` | One invoice with lines |
| PUT | `/api/{company}/invoices/{doc_no}` | Apply an edited line set |

`days` is bounded (1–30) and defaults to 30. The list endpoint sorts rows by
`doc_date` descending server-side, with `doc_no` descending as the tie-break
so same-day invoices come back in a stable order; the client renders the
order it receives. Money serialises as exact decimal strings in every
response, matching the existing endpoints.

The `PUT` request body is a new strict Pydantic model in
`app/models/invoice.py` (`extra="forbid"`, mirroring `InvoiceDraftInput`):

```
InvoiceEditInput:
  company:        CompanyKey
  expected_lines: list[ExpectedLine]   # line set as loaded, for the stale guard
  lines:          list[InvoiceEditLine] # >= 1, the complete desired line set
```

`InvoiceEditLine` carries `item_id`, `quantity` (> 0), `unit_price` (>= 0).
`ExpectedLine` carries the same three fields. `doc_no` comes from the path,
not the body.

`item_id` is AutoCount's product lookup code: the adapter already guarantees
`ProductSummary.id == ProductSummary.code == productCode`, so an edit line's
`item_id` maps directly onto both `InvoiceLineSummary.product_code` (for the
stale guard) and the payload's `productCode`. No separate identifier lookup
is involved.

### Frontend — `app/static/`

`index.html` is 832 lines with all JavaScript inline; three new screens would
push it past 1300. The existing JS is extracted verbatim into
`app/static/app.js` (no behaviour change) and the new screens are added there.

New screens:

1. **Mode choice** — after selecting a company: *New invoice* (today's wizard,
   untouched) or *Recent invoices*.
2. **Recent invoices** — rows showing docNo, date, customer, total, plus a
   cancelled badge; newest first.
3. **Invoice detail** — header and lines, with an *Edit* button, or a short
   explanation of why the invoice is view-only.
4. **Edit** — reuses the items screen: the same line rows with qty/price
   inputs, the same remove button, the same `+ Add item` picker, prefilled
   from the fetched invoice.
5. **Confirm** — a diff before saving: lines added, removed, and changed, and
   the old total → new total.

The success screen offers a way back to the invoice detail view.

## Data flow

```
company → [Recent invoices]
        → GET /api/{company}/invoices?days=30      list_recent_invoices
        → tap a row
        → GET /api/{company}/invoices/{docNo}      get_invoice
        → Edit (only if editable)
        → local line editing, item search reused from the create flow
        → Confirm (diff shown)
        → PUT /api/{company}/invoices/{docNo}
             ├─ get_invoice            (current state)
             ├─ editability guard
             ├─ stale-write guard      (expected_lines vs current)
             ├─ get_item per line      (account-book ownership)
             ├─ client.write PUT       (full desired details array)
             └─ get_invoice            (updated state, returned)
```

## Write hazards

### Stale write

Between loading an invoice and saving it, someone may have changed it in
AutoCount. `expected_lines` carries the line set as the client loaded it; the
service re-reads the invoice immediately before the `PUT` and rejects with
`409 invoice_changed` if the current line set differs. Comparison is on the
ordered tuple of (`product_code`, `qty`, `unit_price`) — exact `Decimal`
comparison, no tolerance. Cost is one extra `GET`; the benefit is that every
applied edit is provably based on current state.

### Ambiguous write

`PUT` goes through `client.write`, so a timeout raises
`AutoCountAmbiguousWriteError` — AutoCount may or may not have applied it.
Because the request is absolute desired state, the service resolves it by
re-reading the invoice with `get_invoice` and comparing the resulting line set
against what was requested:

- **Matches** → the write applied; return success.
- **Does not match** → `409 edit_unconfirmed`, telling the user to re-open the
  invoice and check before retrying.
- **The re-read itself fails** → the original `AutoCountAmbiguousWriteError`
  propagates and surfaces as the existing `502` with `retryable: true`.

## Error handling

New exceptions in `invoice_edit_service.py` with handlers in `app/main.py`,
following the existing structured-envelope style (`error` code, safe
`message`, no internals):

| Status | Error code | Condition |
|---|---|---|
| 404 | `invoice_not_found` | AutoCount has no invoice with that `docNo` |
| 409 | `invoice_not_editable` | Cancelled, or `doc_date` outside the 30-day window |
| 409 | `invoice_changed` | Stale-write guard tripped |
| 409 | `edit_unconfirmed` | Timed-out write could not be confirmed by re-reading |
| 422 | `validation_error` | Malformed request body (existing handler) |
| 502 | `autocount_rejected` | AutoCount refused — locked document, closed period (existing handler, message passed through sanitised) |

An item that does not belong to the selected account book raises the existing
`InvoiceValidationError` → `400 invalid_invoice`.

## Testing

Unit tests only, no live network, following the existing fake-transport
patterns in `tests/unit/test_invoice_listing.py` and
`tests/unit/test_invoice_service.py`.

**Adapter** — `get_invoice` normalisation from a documented view model;
`docNo` mismatch rejected; malformed/non-JSON payload fails closed;
`list_recent_invoices` paging, including the existing inconsistent-total and
repeated-row guards; `description` populated on lines.

**Payload builder** — full array emitted in order; add, remove, and
field-edit cases each produce the expected array; `qty`/`unitPrice` are
`Decimal` instances, never `float` or `str`; `accNo` present on every row; no
`master` key emitted; encoded body contains bare JSON numbers.

**Edit service** — editability guard rejects cancelled and out-of-window
invoices; stale guard rejects a changed line set and accepts an unchanged one;
ambiguous write with a matching re-read succeeds; with a non-matching re-read
raises `edit_unconfirmed`; a failing re-read propagates the ambiguous error;
items from another account book are rejected; `client.write` is called exactly
once per edit.

**API** — each error maps to its documented status and envelope; the three
endpoints are absent from `/openapi.json`; money fields serialise as strings;
`days` outside 1–30 is rejected.

**Live verification** — per `agents.md`, browser-drive the deployed page
against the real account book after the unit suite is green: issue a
throwaway invoice, view it in the list, edit its lines, and confirm the result
in AutoCount's own UI.

## Open questions — resolved by live spike, 2026-08-13

The repo has been burned three times by AutoCount's live behaviour differing
from its documentation (`creditTerm` needing a trailing period, `accNo` being
per-line, the create response being a bare `201` with only a `location`
header). Three assumptions here were undocumented and were confirmed against
one throwaway invoice in the live Sdn Bhd book before any edit-path work.
Full write-up:
[`docs/autocount/invoice-update-spike.md`](../autocount/invoice-update-spike.md).

1. **Does an approved invoice accept `PUT` at all?** **Yes** — `204` against
   an invoice created with `saveApprove: true`. `DELETE` succeeded on one too.
   This was the question that could have voided the design; it did not.
2. **Does omitting `master` preserve the header?** **No — `master` cannot be
   omitted.** The API answers `400 The Master field is required.` The design
   was corrected: echo the five mandatory master fields from the invoice being
   edited and leave the optional ones unsent, which does preserve them
   (`deliverAddress` survived untouched).
3. **Does a shorter `details` array delete the trailing rows?** **Yes** — a
   3-line invoice updated with `[row1, row3]` became exactly those two rows,
   in order. Full-state replace is correct.

## Implementation order

1. Live spike on the three open questions; write up findings.
2. Adapter reads (`get_invoice`, `list_recent_invoices`) + `description` field.
3. Read endpoints (`GET` list, `GET` one) + tests.
4. Payload builder + tests.
5. Edit service with both guards and reconciliation + tests.
6. `PUT` endpoint + error handlers + tests.
7. Frontend: extract `app.js`, then the five screens.
8. Live end-to-end verification through the deployed page.
