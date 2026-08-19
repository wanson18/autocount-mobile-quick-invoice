# AutoCount Cloud Report Print Spike

Date: 2026-08-19
Company: Wanson Enterprise (M) Sdn Bhd
Invoice used: docKey `6447` — taken from the user-provided Cloud report URL
evidence below. NOT re-selected live from the app; no live invoice browse was
performed by the orchestrator.

## Evidence provided by the user (not independently observed)

The user supplied an existing AutoCount Cloud report URL for a Sdn Bhd invoice:

```
https://accounting-report.autocountcloud.com/rpt/<account-book-key>/invoice?reportName=WANSON+SDN+BHD+E-INVOICE+&docKey=6447
```

What this establishes about the configured Cloud report route:

- Host: `accounting-report.autocountcloud.com` (a Cloud report host, distinct
  from the `accounting-api.*` integration API).
- Path shape: `/rpt/<account-book-key>/invoice`. The account-book key is a
  tenant/session-scoped segment and is treated as a server-side configuration
  value — it is NOT committed here or embedded in app code.
- Query parameters: `reportName=WANSON+SDN+BHD+E-INVOICE+` selects the
  configured invoice report layout; `docKey=6447` identifies the invoice by a
  safe document value (the invoice's `docKey`), with no credentials, cookies,
  or debtor/customer secrets in the URL.

This is the same report route AutoCount Cloud itself uses to render/preview the
invoice. It is produced by Cloud, not by this app.

## Cloud menu path

NOT OBSERVED. The orchestrator has no authenticated browser runtime, so the
exact visible AutoCount Cloud menu path ("open invoice → report/preview → Print
/ Export PDF") was not walked. Deferred to the manual Task 4 gate, which runs in
the user's live authenticated session.

## Output check

- Configured Cloud report format: NOT VERIFIED (manual Task 4). The user's
  existing Cloud invoice format is implied by `reportName=WANSON+SDN+BHD+E-INVOICE+`
  in the provided URL, but a visual match was not observed by the orchestrator.
- Cloud Print or Export PDF: NOT VERIFIED (manual Task 4). No Print/Export-PDF
  interaction was performed or observed.
- Stable invoice deep link in the same authenticated session: NOT VERIFIED
  (manual Task 4). The route shape from the provided URL strongly suggests a
  stable, document-identified deep link, but opening it in a fresh authenticated
  tab was not performed by the orchestrator.
- Link contains only a safe document identifier: PASS by inspection of the
  provided URL — the invoice is identified solely by `docKey=6447` (a safe
  document value); no credentials, cookies, or account-book secrets appear in
  the query string. The `<account-book-key>` path segment is redacted here and
  resolved server-side, never committed.

## Decision

**Deep-link handoff** — selected on the basis of the user-provided Cloud report
route. The design hands the user off to the existing Cloud report URL,
constructing the same `/rpt/<account-book-key>/invoice?reportName=...&docKey=...`
shape with the report name and `docKey` filled from the issued invoice and the
account-book key supplied via server-side configuration. This avoids any custom
PDF generation and any automated Cloud HTML scraping, both of which are banned by
the plan and by the prior `pdf-spike.md` (no supported PDF mechanism in the
documented integration API).

Final confirmation of the deep link (live open-in-fresh-tab, format match, Print
/ Export PDF observation) is deferred to the manual Task 4 gate. Until that gate
passes, the deep-link decision is marked provisional on route evidence only.

## Safety result

No invoice write, approval, posting, e-Invoice submission, or automated Cloud UI
scraping was performed. The only artifact is this documentation record plus the
user-provided evidence URL (with account-book key redacted and treated as
server-side config). No test document was created, edited, voided, or submitted.
