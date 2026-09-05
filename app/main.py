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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
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


@dataclass(frozen=True)
class _ErrorResponse:
    """The structured JSON error one domain exception maps to.

    ``extra`` builds the response's additional fields from the exception
    instance (``candidates``, ``status_code``, ``retryable``); ``None`` adds
    none.
    """

    status: int
    code: str
    extra: Callable[[Any], dict[str, Any]] | None = None


# The single source of truth mapping domain exceptions to HTTP error
# responses. FastAPI dispatches on the first registered handler whose class
# matches the raised exception, so subclasses must appear before their base:
# each specific edit failure keeps its own status instead of collapsing into
# InvoiceEditError's 400, and each specific issue failure keeps its own status
# instead of InvoiceServiceError's 409. Each edit failure also gets its own
# code so the client can tell "reopen it" from "this is locked".
_ERROR_RESPONSES: tuple[tuple[type[Exception], _ErrorResponse], ...] = (
    (InvoiceValidationError, _ErrorResponse(400, "invalid_invoice")),
    (InvoiceNotFoundError, _ErrorResponse(404, "invoice_not_found")),
    (InvoiceNotEditableError, _ErrorResponse(409, "invoice_not_editable")),
    (InvoiceChangedError, _ErrorResponse(409, "invoice_changed")),
    (InvoiceEditUnconfirmedError, _ErrorResponse(409, "edit_unconfirmed")),
    (InvoiceEditError, _ErrorResponse(400, "invalid_invoice")),
    (InvoiceIssuePendingError, _ErrorResponse(409, "invoice_pending")),
    (
        InvoiceReconciliationError,
        _ErrorResponse(
            409,
            "invoice_reconciliation_required",
            extra=lambda exc: {"candidates": list(exc.candidates)},
        ),
    ),
    (InvoiceServiceError, _ErrorResponse(409, "invoice_request_failed")),
    (IdempotencyConflictError, _ErrorResponse(409, "idempotency_conflict")),
    (
        AutoCountRejectedError,
        _ErrorResponse(
            502,
            "autocount_rejected",
            extra=lambda exc: {"status_code": exc.status_code},
        ),
    ),
    (
        AutoCountAmbiguousWriteError,
        _ErrorResponse(
            502, "autocount_ambiguous_write", extra=lambda exc: {"retryable": True}
        ),
    ),
    (AutoCountTransportError, _ErrorResponse(502, "autocount_unreachable")),
    (AutoCountDataError, _ErrorResponse(502, "autocount_data_error")),
    (AutoCountUnsupportedError, _ErrorResponse(501, "unsupported")),
    (AutoCountConfigError, _ErrorResponse(500, "server_configuration_error")),
    (AutoCountEndpointError, _ErrorResponse(500, "server_configuration_error")),
    (CompanyConfigError, _ErrorResponse(500, "server_configuration_error")),
)


def _domain_error_handler(
    response: _ErrorResponse,
) -> Callable[[Request, Exception], JSONResponse]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        extra = response.extra(exc) if response.extra is not None else {}
        return _error(response.status, response.code, str(exc), **extra)

    return handler


for _exception_class, _response in _ERROR_RESPONSES:
    app.add_exception_handler(_exception_class, _domain_error_handler(_response))


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
