# Spike: can an already-issued AutoCount invoice be edited?

**Date:** 2026-08-13
**Account book:** Wanson Enterprise (M) Sdn Bhd (`sdn_bhd`)
**Method:** [`scripts/spike_invoice_update.py`](../../scripts/spike_invoice_update.py) via the
[`Invoice update spike`](../../.github/workflows/invoice-update-spike.yml) workflow
**Verdict:** the view-and-edit design holds, with one correction.

## Why this ran before any implementation

`docs/specs/2026-08-13-view-edit-invoices-design.md` rests on three
behaviours the Cloud Accounting Integration API does not document. The first
could void the design outright: every invoice this app issues carries
`saveApprove: true`, and approved documents are commonly locked from edit.
This repo has been burned three times by live behaviour diverging from the
docs (`creditTerm` needing a trailing period, `accNo` being per-line, Create
Invoice answering with a bare `201` and only a `location` header), so the
questions were answered against the live account book before the edit path
was built.

## Findings

Run [31663466403](https://github.com/wanson18/autocount-mobile-quick-invoice/actions/runs/31663466403),
throwaway invoice `CS-034454` (created and deleted within the run):

```
account book : sdn_bhd
invoice used : CS-034454
Q1      : YES -- approved invoice accepted PUT (status 204)
Q2      : YES -- the whole header survived; the unsent optional fields (deliverAddress) were preserved, not blanked
Q3      : YES -- 3 rows became 2; surviving rows are row1 and row3 as sent. Full-state replace works exactly as the design assumes.
cleanup : deleted CS-034454 via DELETE /invoice
```

### Q1 — an approved invoice accepts `PUT`

`PUT /{accountBookId}/invoice?docNo=CS-034454` returned **204** against an
invoice created moments earlier with `saveApprove: true`. Approval does not
lock a document against the integration API in this account book.

Corroborating evidence from the same run and its predecessor: `DELETE
/invoice` also succeeded on an approved invoice.

### Q2 — the header survives, and `master` is mandatory

This started as "does omitting `master` preserve the header?" and the answer
is that **`master` cannot be omitted at all**. A first probe sent only
`details` and was rejected:

```
400 The Master field is required.
```

That is request validation, not a verdict on the invoice's state — it never
reached the approved-document question. `master` is marked required on the
[Invoice Input Model](https://accounting-api.autocountcloud.com/documentation/models/invoice/inputmodels/invoice-inputmodel/).
Update Invoice's "fields that do not need to be updated should be omitted
entirely" applies to fields *within* the models, not to the top-level
`master` object.

The corrected probe echoes back the five documented mandatory master fields,
read from the stored invoice:

```json
{"docDate": "2026-08-13T00:00:00", "debtorCode": "700-0001",
 "debtorName": "TANL MARKETING", "creditTerm": "C.O.D.", "salesLocation": "HQ"}
```

and sends **no optional header field**. After the update, `deliverAddress`
still held its original value (`SPIKE TEST - DELETE ME`) — an unsent optional
field is preserved, not blanked. So a line edit can leave the header alone,
provided the mandatory five are echoed rather than omitted.

Note `docDate` comes back as a datetime (`2026-08-13T00:00:00`) and is echoed
verbatim. Do not reformat it.

### Q3 — a shorter `details` array deletes the trailing rows

A 3-line invoice (`00003`, `00003-BOX`, `00004`) was updated with a 2-row
array carrying rows 1 and 3. The stored invoice became exactly:

```
[('00003', '1.0', '3.3'), ('00004', '1.0', '0.0')]
```

Two rows, holding what was sent, in order. Positional overwrite plus
truncation is confirmed, which is what makes full-state replace correct:
removing the middle line of three means sending rows one and three.

## What this means for the design

**Unchanged:** full-state replace. Every edit sends the complete desired
`details` array, every surviving row spelled out, no `{}` placeholders.
Removal happens by truncation. The request encodes absolute desired state, so
replaying it converges — which is what lets a timed-out write be resolved by
re-reading instead of guessed at.

**Changed:** the header is *not* preserved by omitting `master`. The update
body must carry `master` with the five mandatory fields — `docDate`,
`debtorCode`, `debtorName`, `creditTerm`, `salesLocation` — echoed from the
invoice being edited. Optional header fields stay unsent and survive.

That means the edit path must read the invoice before writing it, which it
already does for the stale-write guard: the same fetch supplies both the
comparison and the master fields to echo.

## Safety notes for re-running

The probe creates one throwaway invoice and deletes it. Every mutation
asserts the document number against the one the run created, so it can never
touch an invoice it did not make. If a run dies between create and delete,
the document number is printed as soon as it exists; re-run the workflow with
`cleanup_only` set to it.

Two earlier runs produced no findings at all and are not evidence of
anything: run 1 (and its re-run) executed before `scripts/` reached the
default branch, and reported success having probed nothing. The workflow now
fails when the probe exits with anything other than "answered yes" or
"answered no".
