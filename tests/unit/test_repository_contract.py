"""The shared idempotency contract is one set of symbols, not two.

Regression pins for a production bug: the Postgres repository used to define
its own ``RequestStatus``, ``IdempotencyError``, ``IdempotencyConflictError``,
and ``InvoiceRequest``, so ``InvoiceService``'s ``request.status is
RequestStatus.SUCCEEDED`` comparisons (and ``main.py``'s 409 handler
registration) matched only the SQLite module's classes. With
``POSTGRES_URL`` set — the production Vercel path — a replay of a succeeded
request fell through to ``InvoiceServiceError`` and an idempotency conflict
fell to the catch-all 500 instead of the registered 409. These tests fail if
the two modules ever hold distinct contract symbols again.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput, InvoiceLineInput
from app.repositories import postgres_request_repository as postgres_repo
from app.repositories import request_repository as sqlite_repo
from app.services.invoice_service import InvoiceService


def test_postgres_module_shares_the_sqlite_contract_symbols():
    assert postgres_repo.RequestStatus is sqlite_repo.RequestStatus
    assert postgres_repo.IdempotencyError is sqlite_repo.IdempotencyError
    assert postgres_repo.IdempotencyConflictError is sqlite_repo.IdempotencyConflictError
    assert postgres_repo.InvoiceRequest is sqlite_repo.InvoiceRequest


def test_to_request_builds_the_same_record_for_both_backends():
    row = {
        "idempotency_key": "draft-key-1",
        "company": "enterprise",
        "request_hash": "hash-a",
        "status": "succeeded",
        "autocount_invoice_id": "doc-key-42",
        "autocount_invoice_number": "INV-2026-0001",
        "error_message": None,
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
    request = postgres_repo.to_request(row)
    assert request is not None
    assert request.status is sqlite_repo.RequestStatus.SUCCEEDED
    assert request.autocount_invoice_id == "doc-key-42"


class _SucceededPostgresShapeRepository:
    """A repository double returning a row built through the shared mapping.

    Models what the wired-in Postgres repository returns on replay of a
    succeeded request: a record constructed by ``to_request`` from stored
    columns. ``InvoiceService`` must return the stored invoice instead of
    raising ``InvoiceServiceError``.
    """

    def begin(self, key, company, request_hash):
        return postgres_repo.to_request(
            {
                "idempotency_key": key,
                "company": company.value,
                "request_hash": request_hash,
                "status": "succeeded",
                "autocount_invoice_id": "doc-key-42",
                "autocount_invoice_number": "INV-2026-0001",
                "error_message": None,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )


def _draft() -> InvoiceDraftInput:
    return InvoiceDraftInput(
        company=CompanyKey.ENTERPRISE,
        invoice_date=date(2026, 9, 1),
        customer_id="CUST-1",
        delivery_address_id="CUST-1:delivery",
        lines=[
            InvoiceLineInput(
                item_id="ITEM-1",
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
                original_unit_price=Decimal("10.00"),
            )
        ],
        submit_einvoice=False,
        idempotency_key="draft-key-1",
    )


@pytest.mark.asyncio
async def test_replay_of_succeeded_postgres_row_returns_stored_invoice():
    service = InvoiceService(
        company_resolver=lambda company: CompanyConfig(
            key=company, name="Wanson Enterprise", account_book_id="book"
        ),
        master_data=None,
        client=None,
        requests=_SucceededPostgresShapeRepository(),
    )

    result = await service.issue(_draft())

    assert result.invoice_id == "doc-key-42"
    assert result.invoice_number == "INV-2026-0001"
