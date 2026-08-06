"""Invoice issue, preview, and PDF endpoints.

- ``POST /api/invoices`` issues one approved invoice through the idempotent
  invoice service; the same idempotency key and payload never creates two
  invoices, and ambiguous timeouts are reconciled before anything else.
- ``POST /api/invoices/preview`` proposes the latest historical price per
  item (with its source invoice) for the confirmation preview.
- ``GET /api/{company}/invoices/{invoice_id}/pdf`` returns the official PDF;
  currently fail-closed ``501`` because AutoCount documents no PDF mechanism
  (see ``docs/autocount/pdf-spike.md``).

Prices are serialised as exact strings, never binary floats.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.config import get_company
from app.dependencies import get_invoice_service, get_master_data
from app.models.company import CompanyKey
from app.models.invoice import (
    InvoiceDraftInput,
    InvoiceIssueResponse,
    InvoicePreviewInput,
    PreviewResponse,
)
from app.services.price_history import get_price_history

router = APIRouter(tags=["invoices"])


@router.post("/invoices", operation_id="issueInvoice", status_code=201, response_model=InvoiceIssueResponse)
async def issue_invoice(
    draft: InvoiceDraftInput,
    service=Depends(get_invoice_service),
) -> dict:
    result = await service.issue(draft)
    return {
        "data": {
            "company": result.company.value,
            "invoice_id": result.invoice_id,
            "invoice_number": result.invoice_number,
            "price_overrides": [
                {
                    "item_id": override["item_id"],
                    "original_unit_price": str(override["original_unit_price"]),
                    "issued_unit_price": str(override["issued_unit_price"]),
                }
                for override in result.price_overrides
            ],
            "einvoice": {
                "status": result.einvoice.status.value,
                "error_message": result.einvoice.error_message,
            },
        }
    }


@router.post("/invoices/preview", operation_id="previewInvoicePrices", response_model=PreviewResponse)
async def preview_invoice_prices(
    preview: InvoicePreviewInput,
    master=Depends(get_master_data),
) -> dict:
    company = get_company(preview.company)
    history = await get_price_history(
        master, company, preview.customer_id, preview.item_ids
    )
    return {
        "data": {
            "customer_id": preview.customer_id,
            "items": [
                {
                    "item_id": item_id,
                    "latest_unit_price": (
                        str(entry.unit_price) if entry is not None else None
                    ),
                    "source_invoice_number": (
                        entry.source_invoice_number if entry is not None else None
                    ),
                    "source_invoice_date": (
                        entry.source_invoice_date if entry is not None else None
                    ),
                }
                for item_id, entry in sorted(history.items())
            ],
        }
    }


@router.get(
    "/{company}/invoices/{invoice_id}/pdf",
    operation_id="getInvoicePdf",
    responses={501: {"description": "AutoCount documents no PDF mechanism"}},
)
async def get_invoice_pdf(
    company: CompanyKey,
    invoice_id: str,
    master=Depends(get_master_data),
) -> Response:
    content = await master.get_invoice_pdf(get_company(company), invoice_id)
    return Response(content=content, media_type="application/pdf")
