"""AutoCount Mobile Quick Invoice — HTTP API.

A narrow HTTPS surface for the private Custom GPT invoice workflow. Every
error is returned as structured JSON; request validation errors carry the
exact field path, service errors carry a safe message, and AutoCount errors
are already sanitised at the client boundary so no credential, account-book
ID, or taxpayer data can leak.

The OpenAPI schema at ``/openapi.json`` is the Custom GPT Action schema:
every operation has a stable ``operationId``, and the invoice issue action is
marked ``x-openai-isConsequential`` so ChatGPT confirms with the user before
calling it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import companies, customers, invoices, print_jobs, products
from app.api.print_jobs import PrintAgentUnauthorizedError
from app.autocount.errors import (
    AutoCountAmbiguousWriteError,
    AutoCountConfigError,
    AutoCountDataError,
    AutoCountEndpointError,
    AutoCountRejectedError,
    AutoCountTransportError,
    AutoCountUnsupportedError,
)
from app.config import CompanyConfigError
from app.repositories.print_job_repository import (
    PrintJobNotFoundError,
    PrintJobStateError,
)
from app.repositories.request_repository import IdempotencyConflictError
from app.services.invoice_edit_service import (
    InvoiceChangedError,
    InvoiceEditError,
    InvoiceEditUnconfirmedError,
    InvoiceNotEditableError,
    InvoiceNotFoundError,
)
from app.services.invoice_service import (
    InvoiceIssuePendingError,
    InvoiceReconciliationError,
    InvoiceServiceError,
    InvoiceValidationError,
)
from app.services.print_jobs import PrintConfigurationError, PrintUnavailableError

app = FastAPI(
    title="AutoCount Mobile Quick Invoice",
    version="0.1.0",
    description=(
        "Private invoice workflow over the AutoCount Cloud Accounting API. "
        "The company is chosen by key; the AutoCount account book is resolved "
        "server-side and never accepted from the client."
    ),
    openapi_url="/openapi.json",
)

app.include_router(companies.router, prefix="/api")
app.include_router(customers.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(invoices.router, prefix="/api")
app.include_router(print_jobs.router, prefix="/api")

class RevalidatingStaticFiles(StaticFiles):
    """Static files the browser must revalidate rather than guess about.

    Starlette sends ``ETag`` and ``Last-Modified`` but no ``Cache-Control``,
    and a response carrying neither ``Cache-Control`` nor ``Expires`` falls
    under the browser's heuristic freshness rule (RFC 9111 4.2.2) -- commonly
    a tenth of the time since ``Last-Modified``. For a page reloaded all day
    that silently serves a stale ``app.js`` after a deploy, which looks
    exactly like the deploy not having shipped.

    ``no-cache`` does not mean "do not store"; it means "revalidate before
    reusing". The ``ETag`` above is what makes that cheap: an unchanged file
    answers ``304`` with no body, so the cost is one conditional request and
    the page can never be older than the deployment serving it.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# Serve the mobile quick-invoice page from the same app/origin as the API, so
# the private deployment is a single Vercel project with no CORS surface to
# reason about. Mounted last: every /api/* route above already matches first,
# so this can never shadow the JSON API. html=True serves index.html for "/".
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", RevalidatingStaticFiles(directory=_STATIC_DIR, html=True), name="static")


def _error(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, **extra},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": [
                {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                for err in exc.errors()
            ],
        },
    )


@app.exception_handler(InvoiceValidationError)
async def invoice_validation_error_handler(
    request: Request, exc: InvoiceValidationError
) -> JSONResponse:
    return _error(400, "invalid_invoice", str(exc))


@app.exception_handler(InvoiceNotFoundError)
async def invoice_not_found_error_handler(
    request: Request, exc: InvoiceNotFoundError
) -> JSONResponse:
    return _error(404, "invoice_not_found", str(exc))


# Each edit failure gets its own code so the client can tell "reopen it" from
# "this is locked". FastAPI dispatches on the exact exception class, and the
# subclasses are registered ahead of the InvoiceEditError base below, so each
# keeps its own status rather than collapsing into the catch-all 400.
@app.exception_handler(InvoiceNotEditableError)
async def invoice_not_editable_error_handler(
    request: Request, exc: InvoiceNotEditableError
) -> JSONResponse:
    return _error(409, "invoice_not_editable", str(exc))


@app.exception_handler(InvoiceChangedError)
async def invoice_changed_error_handler(
    request: Request, exc: InvoiceChangedError
) -> JSONResponse:
    return _error(409, "invoice_changed", str(exc))


@app.exception_handler(InvoiceEditUnconfirmedError)
async def invoice_edit_unconfirmed_error_handler(
    request: Request, exc: InvoiceEditUnconfirmedError
) -> JSONResponse:
    return _error(409, "edit_unconfirmed", str(exc))


@app.exception_handler(InvoiceEditError)
async def invoice_edit_error_handler(
    request: Request, exc: InvoiceEditError
) -> JSONResponse:
    return _error(400, "invalid_invoice", str(exc))


@app.exception_handler(InvoiceIssuePendingError)
async def invoice_pending_error_handler(
    request: Request, exc: InvoiceIssuePendingError
) -> JSONResponse:
    return _error(409, "invoice_pending", str(exc))


@app.exception_handler(InvoiceReconciliationError)
async def invoice_reconciliation_error_handler(
    request: Request, exc: InvoiceReconciliationError
) -> JSONResponse:
    return _error(
        409,
        "invoice_reconciliation_required",
        str(exc),
        candidates=list(exc.candidates),
    )


@app.exception_handler(InvoiceServiceError)
async def invoice_service_error_handler(
    request: Request, exc: InvoiceServiceError
) -> JSONResponse:
    return _error(409, "invoice_request_failed", str(exc))


@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict_error_handler(
    request: Request, exc: IdempotencyConflictError
) -> JSONResponse:
    return _error(409, "idempotency_conflict", str(exc))


@app.exception_handler(AutoCountRejectedError)
async def autocount_rejected_error_handler(
    request: Request, exc: AutoCountRejectedError
) -> JSONResponse:
    return _error(502, "autocount_rejected", str(exc), status_code=exc.status_code)


@app.exception_handler(AutoCountAmbiguousWriteError)
async def autocount_ambiguous_error_handler(
    request: Request, exc: AutoCountAmbiguousWriteError
) -> JSONResponse:
    return _error(502, "autocount_ambiguous_write", str(exc), retryable=True)


@app.exception_handler(AutoCountTransportError)
async def autocount_transport_error_handler(
    request: Request, exc: AutoCountTransportError
) -> JSONResponse:
    return _error(502, "autocount_unreachable", str(exc))


@app.exception_handler(AutoCountDataError)
async def autocount_data_error_handler(
    request: Request, exc: AutoCountDataError
) -> JSONResponse:
    return _error(502, "autocount_data_error", str(exc))


@app.exception_handler(AutoCountUnsupportedError)
async def autocount_unsupported_error_handler(
    request: Request, exc: AutoCountUnsupportedError
) -> JSONResponse:
    return _error(501, "unsupported", str(exc))


@app.exception_handler(AutoCountConfigError)
async def autocount_config_error_handler(
    request: Request, exc: AutoCountConfigError
) -> JSONResponse:
    return _error(500, "server_configuration_error", str(exc))


@app.exception_handler(AutoCountEndpointError)
async def autocount_endpoint_error_handler(
    request: Request, exc: AutoCountEndpointError
) -> JSONResponse:
    return _error(500, "server_configuration_error", str(exc))


@app.exception_handler(CompanyConfigError)
async def company_config_error_handler(
    request: Request, exc: CompanyConfigError
) -> JSONResponse:
    return _error(500, "server_configuration_error", str(exc))


@app.exception_handler(PrintUnavailableError)
async def print_unavailable_error_handler(
    request: Request, exc: PrintUnavailableError
) -> JSONResponse:
    return _error(501, "unsupported", str(exc))


@app.exception_handler(PrintConfigurationError)
async def print_configuration_error_handler(
    request: Request, exc: PrintConfigurationError
) -> JSONResponse:
    return _error(500, "server_configuration_error", str(exc))


@app.exception_handler(PrintAgentUnauthorizedError)
async def print_agent_unauthorized_error_handler(
    request: Request, exc: PrintAgentUnauthorizedError
) -> JSONResponse:
    return _error(401, "unauthorized", "print agent token is missing or invalid")


@app.exception_handler(PrintJobNotFoundError)
async def print_job_not_found_error_handler(
    request: Request, exc: PrintJobNotFoundError
) -> JSONResponse:
    return _error(404, "print_job_not_found", str(exc))


@app.exception_handler(PrintJobStateError)
async def print_job_state_error_handler(
    request: Request, exc: PrintJobStateError
) -> JSONResponse:
    return _error(409, "print_job_conflict", str(exc))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error(500, "internal_error", "an unexpected error occurred")


def custom_openapi() -> dict:
    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        servers=[
            {"url": "https://autocount-mobile-quick-invoice.vercel.app", "description": "Production"},
        ],
    )
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
