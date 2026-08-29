# Scripts

Diagnostic and spike scripts that talk to AutoCount. They are **not** served
by Vercel. Credentials come from the environment (see `.env.example`).

| Script | Purpose |
|---|---|
| [`diagnose_invoice_listing.py`](diagnose_invoice_listing.py) | Read-only diagnosis of AutoCount invoice listing paging |
| [`spike_invoice_update.py`](spike_invoice_update.py) | Live probe of whether an approved invoice accepts `PUT` |

Office printing is not handled here. Open the official AutoCount Cloud
report from the PWA (**Open Cloud Report**), then print with the Epson app.
