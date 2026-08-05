# AutoCount Official Invoice PDF — Technical Spike (2026-08-05)

**Result: unsupported.** The AutoCount Cloud Accounting Integration API
(`accounting-api.autocountcloud.com`) exposes no documented mechanism for
retrieving the official invoice PDF. Sharing stays disabled until a supported
mechanism is confirmed.

## What was checked

The documentation site index
(`/documentation/category/api-references`) lists 23 API categories:
Credit Note, Invoice, Journal Entry, Payment, Purchase Invoice, Purchase
Order, Purchase Return, Quotation, Knock Off Entry, Stock Adjustment, Stock
Transfer, Creditor, Debtor, Product, Tax Entity, Account, Area, Company
Profile, Department, DocNo Format, Location, Payment Method, Sales Agent.

No print, PDF, report, download, attachment, or file category exists.

The Invoice category has exactly eight methods:

| Method | Endpoint |
|--------|----------|
| Get Invoice Listing (Simple) | `GET /{accountBookId}/invoice/listing` |
| Get Invoice Listing (Specific) | `POST /{accountBookId}/invoice/listing` |
| Get Invoice | `GET /{accountBookId}/invoice?docNo=...` |
| Create Invoice | `POST /{accountBookId}/invoice` |
| Update Invoice | `PUT /{accountBookId}/invoice` |
| Delete Invoice | `DELETE /{accountBookId}/invoice` |
| Get Invoice Knock Off Details | invoice knock-off method |
| Void Invoice | void method |

`GET /{accountBookId}/invoice` returns the documented Invoice View Model
(`master` + `details`) as plain JSON only. The Invoice Master View Model has
no PDF, print, or download fields (its only URLs are e-Invoice related:
`eInvoiceValidationLink`, `eInvoiceUuid`). No response anywhere includes
PDF bytes, a print URL, or an authenticated download URL.

## Why not the alternatives

- **Production HTML scraping** of the AutoCount Cloud web portal is banned by
  the implementation plan: "Do not use production HTML scraping when no
  supported mechanism exists."
- **Reproducing the official layout ourselves** is banned unless AutoCount
  offers no supported method *and* a separate fallback is explicitly
  approved (design spec §9).
- **Third-party plugins** (for example the A036 Invoicing API on the AutoCount
  Plugin Portal) are not part of the documented Cloud API and require their
  own deployment; out of scope for this spike.

## Adapter contract

`app/autocount/adapter.py` exposes:

```python
async def get_invoice_pdf(company, invoice_id) -> bytes:
```

It currently raises `AutoCountUnsupportedError` without making an HTTP call,
so every caller (REST route, GPT action, PWA) fails closed with a clear
"unsupported" message and sharing remains disabled.

When AutoCount documents a supported PDF/print/download mechanism, this
method is the single place to change: call the documented endpoint through
`AutoCountClient.read`, validate the response starts with `%PDF`, and return
the bytes. The unit tests in `tests/unit/test_invoice_pdf.py` already encode
the intended contract and can be extended with the confirmed endpoint.

## Status

- [x] Spike: no supported PDF mechanism in the documented Cloud API
- [ ] AutoCount confirms a supported mechanism (feature request / newer API)
- [ ] `get_invoice_pdf` implemented against the confirmed endpoint
- [ ] PDF sharing enabled end to end
