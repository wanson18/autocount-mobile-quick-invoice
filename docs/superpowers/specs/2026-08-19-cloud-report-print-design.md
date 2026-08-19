# Cloud Report Print Handoff Design

**Date:** 2026-08-19
**Status:** Proposed; implementation is gated by the live Cloud-report check
**Branch:** `codex/cloud-report-print-test`

## Goal

After an invoice has been issued with `saveApprove: true`, let the user reach
the same invoice in AutoCount Cloud and use the user's configured Cloud invoice
report to print, export, or share it. The app must not replace that report with
a locally generated layout.

## Context

The mobile app already returns the AutoCount-generated invoice number after the
create-and-read-back flow. The current PDF endpoint fails closed because the
documented Cloud Integration API exposes no invoice PDF or print mechanism; the
finding is recorded in `docs/autocount/pdf-spike.md`.

AutoCount Cloud itself remains the authority for the invoice report format. The
feature therefore has two possible implementation outcomes:

1. If AutoCount confirms a supported report/PDF endpoint, the existing PDF
   route can retrieve those exact bytes and the PWA can print/share them.
2. If no supported endpoint exists, the PWA will hand the user to AutoCount
   Cloud. It may open a verified invoice deep link, or open the Cloud app and
   copy the invoice number for search. The user then uses Cloud's own
   Print/Export/Share controls.

Only one outcome is implemented after the live check. Production HTML scraping,
custom reproduction of the Cloud report, draft invoices, e-Invoice submission,
and automatic WhatsApp sending are out of scope.

## Required behavior

- The normal issue flow remains unchanged: `saveApprove: true` and
  `submitEInvoice: false`.
- Printing or sharing is a post-issue action and never creates or updates an
  invoice.
- The target is looked up by the server-confirmed invoice document number, not
  by stale client draft data. The Cloud report URL is then built from the
  server-confirmed AutoCount document key (`docKey`), because the supplied
  Cloud route uses `docKey=6447`, not the invoice number.
- Company/account-book isolation remains server-side; no account-book ID,
  API key, or taxpayer credential may be placed in browser assets or public app
  JSON. The final browser navigation may display AutoCount's own
  server-configured account-book path because Cloud requires that path to
  resolve the report.
- A handoff action must be labelled honestly as `Open Cloud Report` or
  `Open AutoCount Cloud` unless the app has retrieved an actual PDF file.
- Cancelling the Cloud tab, print dialog, export, or share sheet does not alter
  the issued invoice result.

## Decision gate

The first task uses an existing issued invoice and records:

- the Cloud menu path that opens the configured invoice report;
- whether a deep link can reopen that invoice in an already-authenticated
  browser session;
- whether the query identifies the invoice with the safe server-confirmed
  document key, while the account-book path remains server configuration and
  is not copied into app assets or client-generated URLs;
- whether the report's Print/Export PDF output is the user's configured format.

If those checks fail, the implementation uses the generic Cloud handoff and
invoice-number copy fallback. It does not attempt browser scraping or invent a
PDF endpoint.

## Acceptance criteria

- A real issued invoice can be opened in AutoCount Cloud from the result flow,
  either directly or through the generic Cloud handoff.
- The Cloud report shown is the user's existing configured report.
- The app never creates a second invoice during print/share attempts.
- The PWA contains no AutoCount credential or account-book ID; any required
  account-book path appears only in the final AutoCount Cloud destination URL.
- Existing invoice creation, price history, read-back, and e-Invoice-disabled
  behavior remain unchanged.
