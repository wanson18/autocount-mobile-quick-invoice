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

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.autocount.errors import AutoCountRejectedError
from app.config import get_company
from app.dependencies import (
    get_invoice_edit_service,
    get_invoice_service,
    get_master_data,
)
from app.models.company import CompanyKey
from app.models.invoice import (
    EInvoiceResultItem,
    InvoiceDraftInput,
    InvoiceEditInput,
    InvoiceIssueData,
    InvoiceIssueResponse,
    InvoicePreviewInput,
    PreviewData,
    PreviewItem,
    PreviewResponse,
    PriceOverrideItem,
)
from app.models.master_data import (
    InvoiceDetailItem,
    InvoiceDetailResponse,
    InvoiceLineItem,
    InvoiceListItem,
    InvoiceListResponse,
    InvoiceSummary,
)
from app.services.invoice_edit_service import (
    EDIT_WINDOW_DAYS,
    InvoiceNotFoundError,
    is_editable,
)
from app.services.price_history import get_price_history

router = APIRouter(tags=["invoices"])

#: How far back the recent-invoice list looks by default. Deliberately much
#: shorter than ``EDIT_WINDOW_DAYS``: browsing is for "what did I just issue",
#: where a short list is faster to scan and cheaper to fetch, while an invoice
#: stays correctable for far longer. The ``days`` parameter opens the window
#: back out to the edit horizon when an older invoice needs reaching.
LIST_WINDOW_DAYS = 3


@router.post(
    "/invoices",
    operation_id="issueInvoice",
    status_code=201,
    response_model=InvoiceIssueResponse,
    openapi_extra={"x-openai-isConsequential": True},
)
async def issue_invoice(
    draft: InvoiceDraftInput,
    service=Depends(get_invoice_service),
) -> InvoiceIssueResponse:
    result = await service.issue(draft)
    return InvoiceIssueResponse(
        data=InvoiceIssueData(
            company=result.company,
            invoice_id=result.invoice_id,
            invoice_number=result.invoice_number,
            price_overrides=[
                PriceOverrideItem(
                    item_id=override["item_id"],
                    original_unit_price=str(override["original_unit_price"]),
                    issued_unit_price=str(override["issued_unit_price"]),
                )
                for override in result.price_overrides
            ],
            einvoice=EInvoiceResultItem(
                status=result.einvoice.status.value,
                error_message=result.einvoice.error_message,
            ),
        )
    )


@router.post("/invoices/preview", operation_id="previewInvoicePrices", response_model=PreviewResponse)
async def preview_invoice_prices(
    preview: InvoicePreviewInput,
    master=Depends(get_master_data),
) -> PreviewResponse:
    company = get_company(preview.company)
    history = await get_price_history(
        master, company, preview.customer_id, preview.item_ids
    )
    return PreviewResponse(
        data=PreviewData(
            customer_id=preview.customer_id,
            items=[
                PreviewItem(
                    item_id=item_id,
                    latest_unit_price=(
                        str(entry.unit_price) if entry is not None else None
                    ),
                    source_invoice_number=(
                        entry.source_invoice_number if entry is not None else None
                    ),
                    source_invoice_date=(
                        entry.source_invoice_date if entry is not None else None
                    ),
                )
                for item_id, entry in sorted(history.items())
            ],
        )
    )


@router.get(
    "/{company}/invoices",
    response_model=InvoiceListResponse,
    include_in_schema=False,
)
async def list_invoices(
    company: CompanyKey,
    days: int = Query(default=LIST_WINDOW_DAYS, ge=1, le=EDIT_WINDOW_DAYS),
    master=Depends(get_master_data),
) -> InvoiceListResponse:
    """Recently issued invoices for the selected company, newest first.

    Hidden from the OpenAPI schema on purpose: that schema is the Custom GPT
    Action contract, and browsing invoices is a mobile-page workflow.

    Defaults to the short browse window and is capped at the edit window,
    which is as far back as this app can act on an invoice anyway.
    """
    today = date.today()
    invoices = await master.list_recent_invoices(
        get_company(company),
        date_from=(today - timedelta(days=days)).isoformat(),
        date_to=today.isoformat(),
    )
    return InvoiceListResponse(
        data=[
            InvoiceListItem(
                id=invoice.id,
                doc_no=invoice.doc_no,
                doc_date=invoice.doc_date,
                debtor_code=invoice.debtor_code,
                debtor_name=invoice.debtor_name,
                total=str(invoice.total),
                is_cancelled=invoice.is_cancelled,
                line_count=len(invoice.lines),
            )
            for invoice in invoices
        ]
    )


@router.get(
    "/{company}/invoices/{doc_no}",
    response_model=InvoiceDetailResponse,
    include_in_schema=False,
)
async def get_invoice_detail(
    company: CompanyKey,
    doc_no: str,
    master=Depends(get_master_data),
) -> InvoiceDetailResponse:
    invoice = await _read_invoice(master, get_company(company), doc_no)
    return InvoiceDetailResponse(data=_detail_item(invoice))


@router.put(
    "/{company}/invoices/{doc_no}",
    response_model=InvoiceDetailResponse,
    include_in_schema=False,
)
async def update_invoice(
    company: CompanyKey,
    doc_no: str,
    edit: InvoiceEditInput,
    service=Depends(get_invoice_edit_service),
) -> InvoiceDetailResponse:
    """Replace an issued invoice's line set with the confirmed desired state.

    Hidden from the OpenAPI schema like the read endpoints: editing a live
    invoice is a mobile-page workflow, never a Custom GPT action. The document
    number comes from the path and the body carries no header field, so an
    edit can only ever change lines.
    """
    updated = await service.edit(doc_no, edit)
    return InvoiceDetailResponse(data=_detail_item(updated))


async def _read_invoice(master, company, doc_no: str) -> InvoiceSummary:
    """Fetch one invoice, turning AutoCount's 404 into a domain error.

    Any other upstream status keeps propagating as ``AutoCountRejectedError``
    so a real upstream failure still surfaces as a sanitised 502 rather than
    being mistaken for a missing invoice.
    """
    try:
        return await master.get_invoice(company, doc_no)
    except AutoCountRejectedError as exc:
        if exc.status_code == 404:
            raise InvoiceNotFoundError(
                f"no invoice {doc_no!r} in the selected company"
            ) from None
        raise


def _detail_item(invoice: InvoiceSummary) -> InvoiceDetailItem:
    return InvoiceDetailItem(
        id=invoice.id,
        doc_no=invoice.doc_no,
        doc_date=invoice.doc_date,
        debtor_code=invoice.debtor_code,
        debtor_name=invoice.debtor_name,
        total=str(invoice.total),
        is_cancelled=invoice.is_cancelled,
        is_editable=is_editable(invoice),
        lines=[
            InvoiceLineItem(
                product_code=line.product_code,
                description=line.description,
                quantity=str(line.qty),
                unit_price=str(line.unit_price),
            )
            for line in invoice.lines
        ],
    )


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
