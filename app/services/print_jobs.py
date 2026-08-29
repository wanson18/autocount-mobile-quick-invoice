"""Enqueue an office print job for an issued invoice.

The mobile client supplies only the company key and document number. This
service looks the invoice up in the selected account book, builds the official
AutoCount Cloud report URL from the server-side template and the confirmed
``docKey``, and stores a queued job. The Cloud URL is never returned to the
phone; only the trusted Windows agent receives it after claiming the job.

Fails closed when the Cloud template or office printer name is missing, and
when the template is present but invalid. Does not invent a PDF API call and
does not scrape Cloud HTML.
"""

from __future__ import annotations

from typing import Protocol

from app.config import (
    CompanyConfig,
    get_cloud_invoice_url_template,
    get_office_printer_name,
    get_print_job_claim_lease_seconds,
)
from app.models.company import CompanyKey
from app.models.master_data import InvoiceSummary
from app.repositories.print_job_repository import PrintJob
from app.services.cloud_report_link import build_cloud_report_url
from app.services.invoice_edit_service import read_invoice


class PrintUnavailableError(Exception):
    """Cloud report template or office printer is not configured."""


class PrintConfigurationError(Exception):
    """Print configuration is present but invalid."""


class PrintJobRepositoryPort(Protocol):
    def enqueue(
        self,
        *,
        company: CompanyKey,
        doc_no: str,
        cloud_report_url: str,
        printer_name: str,
        job_id: str | None = None,
    ) -> PrintJob: ...

    def get(self, job_id: str) -> PrintJob | None: ...

    def claim_next(self, *, lease_seconds: int = 600) -> PrintJob | None: ...

    def mark_printed(self, job_id: str) -> PrintJob: ...

    def mark_failed(self, job_id: str, message: str) -> PrintJob: ...


class MasterDataPort(Protocol):
    async def get_invoice(
        self, company: CompanyConfig, invoice_no: str
    ) -> InvoiceSummary: ...


async def enqueue_print_job(
    *,
    master: MasterDataPort,
    jobs: PrintJobRepositoryPort,
    company: CompanyConfig,
    doc_no: str,
) -> PrintJob:
    """Create a queued print job for a server-confirmed invoice."""
    template = get_cloud_invoice_url_template()
    if not template:
        raise PrintUnavailableError(
            "Cloud report URL is not configured on the server"
        )
    printer_name = get_office_printer_name()
    if not printer_name:
        raise PrintUnavailableError(
            "Office printer is not configured on the server"
        )
    invoice = await read_invoice(master, company, doc_no)
    try:
        url = build_cloud_report_url(template, invoice.id)
    except ValueError:
        raise PrintConfigurationError(
            "Cloud report URL configuration is invalid"
        ) from None
    return jobs.enqueue(
        company=company.key,
        doc_no=invoice.doc_no,
        cloud_report_url=url,
        printer_name=printer_name,
    )


def claim_next_print_job(jobs: PrintJobRepositoryPort) -> PrintJob | None:
    return jobs.claim_next(lease_seconds=get_print_job_claim_lease_seconds())
