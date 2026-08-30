# Project: AutoCount Mobile Quick Invoice

## Rules & Constraints

- **Strict Scope:** Focus on mobile invoice creation, AutoCount entry, and the
  e-Invoice flow. Do not add unapproved features.
- **Cloud report, not a homemade PDF.** After issue (and from Recent invoice
  detail), the phone offers **Open Cloud Report**: a hidden same-origin
  redirect to the official AutoCount Cloud report URL. Never return
  account-book IDs or the Cloud report URL to the mobile client. There is no
  AutoCount PDF API — do not invent one, scrape Cloud HTML, or generate a
  homemade invoice layout. Office printing is done in AutoCount Cloud (then
  the Epson app), not by a Windows print-agent queue in this repo.
- **Company-scoped Cloud routing.** The server resolves the selected
  `CompanyKey` to its own account book and report name, then uses the
  server-confirmed invoice `docKey`. Never reuse one global Cloud URL template
  for both Wanson Enterprise and Wanson Sdn Bhd.
- **Workflow:** Implement code task-by-task based on `autocount_mobile_invoice_v1_plan.md` (see also `autocount_mobile_invoice_v1_spec.md` for requirements).
- **Testing:** Always run `pytest tests/` before committing. Prefer adding a regression test over a one-off manual check.
- **Git Strategy:** Small, clean commits with a message that states the root cause, not just the symptom (see commit history from 2026-08 for the pattern).
- **Root-cause via official docs, not guessing.** When AutoCount rejects a request, consult the official docs at
  `https://accounting-api.autocountcloud.com/documentation/` before changing values speculatively. Guessing burns the
  user's live account and their patience. Only ask the user for a value when it is genuinely business-specific data
  that only their AutoCount instance holds (e.g. an exact GL account code or credit-term key); if they hand you a
  screenshot or other ground truth, use it directly without further questions.

## Known AutoCount data-model gotchas (learned the hard way — do not re-derive)

These were each root-caused against a live AutoCount account book (Wanson Enterprise (M) Sdn Bhd) and are encoded in
`app/autocount/mapping.py` and `app/services/invoice_service.py`. Read those files' docstrings/comments for full
citations before touching invoice payload or response-parsing code.

- **`accNo` lives on each invoice *line* (Invoice Detail Input Model), not the invoice master.** The master model has
  no `accNo` field at all. It is the Sales GL / Chart-of-Accounts code, unrelated to the debtor's own `accNo` (a
  different field on customer/debtor records — don't confuse the two).
- **`creditTerm` must exactly match an existing `CreditTermKey`** configured in AutoCount's Master Data > Credit Term
  screen — case- and punctuation-exact. For this account book the real value is `"C.O.D."` (with a trailing period);
  `"COD"` and `"C.O.D"` are both rejected live with `CreditTerm (CreditTermKey = ...) not exists`. If this ever needs
  to change, get the exact string from a screenshot of the AutoCount dropdown, not a guess.
- **Create Invoice's documented success response is a bare `201` with no guaranteed JSON body.** The only documented
  success signal is a `location` response header. Do not assume `{"data": {...}}` — parse `docNo` out of the
  `location` header's query string, then call Get Invoice (`GET /invoice?docNo=...`) to fetch the full view model and
  read `master.docKey` (the invoice's true identity) and `master.docNo`. See
  `InvoiceService._resolve_created_invoice` for the implementation.
- **`autoFillOption.accNo: true`** is documented to auto-resolve `accNo` from AutoCount's "Product Posting" master
  data config, but that isn't set up for Wanson's products — `accNo` must still be sent explicitly on every line.

## Verification

- Live end-to-end verification is done via Chrome browser automation against the deployed Vercel URL, walking the
  actual mobile UI (Company → Customer → Items → Review → Issue Invoice) — not just unit tests. AutoCount's
  documented contract and its live behavior have diverged before (see the response-parsing gotcha above), so a green
  test suite alone does not prove the flow works against the real API.
- After any change to `app/autocount/mapping.py` or `app/services/invoice_service.py`, re-run the full suite
  (`pytest tests/`) and, if the change affects the create/response path, re-verify live before considering the fix
  done.
