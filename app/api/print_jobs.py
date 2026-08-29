"""Office print-job endpoints.

Mobile:

- ``POST /api/{company}/invoices/{doc_no}/print`` enqueues a job.
- ``GET /api/{company}/invoices/{doc_no}/print/{job_id}`` returns status.

Windows agent (``Authorization: Bearer $PRINT_AGENT_TOKEN``):

- ``POST /api/print-agent/jobs/next`` claims the oldest queued job.
- ``POST /api/print-agent/jobs/{job_id}/complete`` reports printed or failed.

Every route is hidden from the Custom GPT OpenAPI schema. Mobile responses
never include the Cloud report URL, account-book path, or agent token.
"""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import (
    get_company,
    get_print_agent_token,
)
from app.dependencies import get_master_data, get_print_job_repository
from app.models.common import ItemResponse
from app.models.company import CompanyKey
from app.repositories.print_job_repository import (
    PrintJobNotFoundError,
    PrintJobStateError,
)
from app.services.print_jobs import (
    PrintConfigurationError,
    claim_next_print_job,
    enqueue_print_job,
)

router = APIRouter(tags=["print"], include_in_schema=False)


class PrintAgentUnauthorizedError(Exception):
    """The print-agent bearer token is missing or does not match."""


class PrintJobPublic(BaseModel):
    job_id: str
    company: CompanyKey
    doc_no: str
    status: str
    error_message: str | None = None


class PrintJobAgentView(PrintJobPublic):
    cloud_report_url: str
    printer_name: str


PrintJobPublicResponse = ItemResponse[PrintJobPublic]
PrintJobAgentResponse = ItemResponse[PrintJobAgentView]


class PrintJobCompleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["printed", "failed"]
    error_message: str | None = Field(default=None)

    @model_validator(mode="after")
    def failed_jobs_need_a_message(self) -> PrintJobCompleteInput:
        if self.status == "failed" and not (self.error_message or "").strip():
            raise ValueError("error_message is required when status is failed")
        if self.status == "printed":
            return self.model_copy(update={"error_message": None})
        return self


def require_print_agent_token(
    authorization: str | None = Header(default=None),
) -> None:
    expected = get_print_agent_token()
    if not expected:
        raise PrintConfigurationError("PRINT_AGENT_TOKEN is not configured")
    scheme, _, value = (authorization or "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not value
        or not _tokens_match(value, expected)
    ):
        raise PrintAgentUnauthorizedError()


def _tokens_match(given: str, expected: str) -> bool:
    if len(given) != len(expected):
        return False
    return secrets.compare_digest(given, expected)


@router.post(
    "/{company}/invoices/{doc_no}/print",
    include_in_schema=False,
    status_code=201,
    response_model=PrintJobPublicResponse,
)
async def enqueue_invoice_print(
    company: CompanyKey,
    doc_no: str,
    master=Depends(get_master_data),
    jobs=Depends(get_print_job_repository),
) -> PrintJobPublicResponse:
    """Queue the official Cloud invoice report for the office Epson."""
    job = await enqueue_print_job(
        master=master,
        jobs=jobs,
        company=get_company(company),
        doc_no=doc_no,
    )
    return PrintJobPublicResponse(data=PrintJobPublic(**job.public_dict()))


@router.get(
    "/{company}/invoices/{doc_no}/print/{job_id}",
    include_in_schema=False,
    response_model=PrintJobPublicResponse,
)
async def get_invoice_print_job(
    company: CompanyKey,
    doc_no: str,
    job_id: str,
    jobs=Depends(get_print_job_repository),
) -> PrintJobPublicResponse:
    """Return mobile-safe print-job status. Never includes the Cloud URL."""
    job = jobs.get(job_id)
    if job is None or job.company is not company or job.doc_no != doc_no:
        raise PrintJobNotFoundError(f"no print job {job_id!r}")
    return PrintJobPublicResponse(data=PrintJobPublic(**job.public_dict()))


@router.post(
    "/print-agent/jobs/next",
    include_in_schema=False,
    response_model=PrintJobAgentResponse,
    responses={204: {"description": "No queued print job"}},
)
async def claim_next_job(
    jobs=Depends(get_print_job_repository),
    _: None = Depends(require_print_agent_token),
) -> PrintJobAgentResponse | Response:
    """Claim the oldest queued job, including the resolved Cloud report URL."""
    job = claim_next_print_job(jobs)
    if job is None:
        return Response(status_code=204)
    return PrintJobAgentResponse(data=PrintJobAgentView(**job.agent_dict()))


@router.post(
    "/print-agent/jobs/{job_id}/complete",
    include_in_schema=False,
    response_model=PrintJobAgentResponse,
)
async def complete_job(
    job_id: str,
    body: PrintJobCompleteInput,
    jobs=Depends(get_print_job_repository),
    _: None = Depends(require_print_agent_token),
) -> PrintJobAgentResponse:
    """Record that the office agent printed or failed the claimed job."""
    if body.status == "printed":
        job = jobs.mark_printed(job_id)
    else:
        job = jobs.mark_failed(job_id, body.error_message or "print failed")
    return PrintJobAgentResponse(data=PrintJobAgentView(**job.agent_dict()))


# Re-export for the app-level exception handlers.
__all__ = [
    "router",
    "PrintAgentUnauthorizedError",
]
