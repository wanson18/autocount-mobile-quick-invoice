# Why browsing invoices failed: "repeated a record across pages"

**Date:** 2026-08-13
**Account book:** Wanson Enterprise (M) Sdn Bhd (`sdn_bhd`)
**Method:** [`scripts/diagnose_invoice_listing.py`](../../scripts/diagnose_invoice_listing.py) via the
[`Invoice listing diagnostic`](../../.github/workflows/invoice-listing-diagnostic.yml) workflow,
[run 31715378750](https://github.com/wanson18/autocount-mobile-quick-invoice/actions/runs/31715378750)
**Verdict:** AutoCount is fine. The adapter's guard was too strict for a browse.

## The failure

Loading the recent-invoice list failed live with:

```
Could not load invoices: AutoCount listing repeated a record across pages
```

That is `AutoCountMasterDataAdapter._listing`'s own guard: paging collected
the same `docKey` twice.

## What the diagnostic found

```
[1] UNFILTERED listing, page 1 (baseline book size)
    totalCount        : 5756
    rows on page 1    : 100
    first/last docDate: 2026-07-04 .. 2026-01-02

[2] DATE-FILTERED listing, page 1  filter={'date': {'from': '2026-08-10', 'to': '2026-08-13'}}
    totalCount        : 126
    rows on page 1    : 100
    >>> the date filter DID narrow the result set
    >>> every row on page 1 falls inside the window

[3] PAGE 2 comparison
    rows on page 2    : 26
    docKeys on both   : 0
    >>> pages are disjoint -- paging itself looks correct

[4] STABILITY: refetching page 1 to see if the order changes
    >>> page 1 returned the same rows in the same order
```

All three suspected causes are ruled out. The filter narrows correctly,
`page` is honoured, `100 + 26 = 126 = totalCount`, and the pages are
disjoint. Replayed against this data the adapter would have succeeded.

## The actual cause

Two facts from that output combine into it.

**The window spans more than one page.** 126 invoices in three days against a
[documented 100-record page](https://accounting-api.autocountcloud.com/documentation/models/invoice/inputmodels/invoice-listing-inputmodel)
means every browse pages. This is a busy book — roughly 42 invoices a day —
so the multi-page path is the normal path, not an edge case. `search_invoices`
never hit this because its `debtorCode` filter narrows to a handful of rows
that fit on page 1, so it never requests a second page.

**The listing has no stable order.** Line `[1]` is the tell: unfiltered page 1
runs `2026-07-04 .. 2026-01-02` while the book holds invoices dated
`2026-08-12`. It is neither newest- nor oldest-first, so the order is
incidental rather than guaranteed. Paging an unordered result set is only
stable while the set is untouched — which is exactly why `[4]` passed, having
refetched the same page milliseconds apart with nothing being written.

Put together: a write landing between the page 1 and page 2 requests shifts
the boundary, and a row that was last on page 1 comes back first on page 2.
The diagnostic ran at a quiet moment and saw a clean snapshot; the live app,
reading a book being written to all day, does not always get one.

Note this cannot be a concurrent *insert* into the window — that would change
`totalCount` between pages and trip the inconsistent-total guard first, which
raises a different error. A reorder that leaves the count alone is what
produces this one.

## The fix

`_listing` gains an `on_repeat` policy:

- **`"fail"`** (default, unchanged) — a repeat is a contract violation.
  `search_invoices` keeps it, because ambiguous-write reconciliation matches
  invoices by identity and must not act on a listing it cannot trust.
- **`"skip"`** — the duplicate is dropped and paging continues. Used only by
  `list_recent_invoices`. Paging also stops when a page contributes nothing
  new, since a page that only repeats what is already collected cannot be
  advanced past.

A browse list does not need to be exact to be useful, and showing the
operator an error instead of their invoices is strictly worse than showing
them a list that is one row short during a burst of writing.

## What this does not do

It does not make the listing ordered — AutoCount's listing input model
exposes only `page` and `filter`, with no sort field, so there is nothing to
request. `list_recent_invoices` still sorts what it collects by
`(doc_date, doc_no)` before returning, so the rendered order is stable even
though the fetch order is not.

It also does not address the window holding more rows than one page. If the
browse list should be capped rather than complete, that is a product
decision, not a paging one.
