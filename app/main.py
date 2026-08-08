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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import companies, customers, invoices, products
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
from app.repositories.request_repository import IdempotencyConflictError
from app.services.invoice_service import (
    InvoiceIssuePendingError,
    InvoiceReconciliationError,
    InvoiceServiceError,
    InvoiceValidationError,
)

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

# Serve the mobile quick-invoice page from the same app/origin as the API, so
# the private deployment is a single Vercel project with no CORS surface to
# reason about. Mounted last: every /api/* route above already matches first,
# so this can never shadow the JSON API. html=True serves index.html for "/".
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


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
