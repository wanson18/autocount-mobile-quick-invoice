"""Postgres-backed idempotency repository for invoice issue requests.

Same contract and rules as ``app.repositories.request_repository.RequestRepository``
(SQLite), reimplemented against Postgres so it survives a serverless platform's
ephemeral filesystem (e.g. Vercel), where a local SQLite file does not persist
between invocations.

- Same idempotency key plus the same request body returns the stored result.
- Same idempotency key with a different request body is rejected with
  ``IdempotencyConflictError`` before any AutoCount call is made.
- Rows are inserted as ``pending``. A later ``succeeded`` row is the
  authoritative result; ``pending`` and ``ambiguous`` rows are returned as-is
  so the invoice service reconciles them before replaying.
- Price overrides are stored separately as non-authoritative audit metadata.
- ``begin`` uses ``INSERT ... ON CONFLICT DO NOTHING`` so a concurrent
  double-tap from two mobile requests cannot create two rows for one key;
  Postgres's row-level locking on the conflicting key serialises the writers.

This is request metadata only, never a ledger: it is never authoritative for
accounting data.

Requires the ``psycopg`` package (psycopg 3), sync driver — a FastAPI request
already awaits a thread-pool-bound sync call for the AutoCount HTTP client
boundary at this project's current scale, so a sync driver here keeps the
dependency surface small. Swap for ``psycopg_pool`` if connection volume grows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.models.company import CompanyKey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_requests (
  idempotency_key TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  autocount_invoice_id TEXT,
  autocount_invoice_number TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_price_overrides (
  id BIGSERIAL PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  autocount_invoice_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  original_unit_price TEXT NOT NULL,
  issued_unit_price TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (idempotency_key, item_id)
);
"""


class RequestStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class IdempotencyError(Exception):
    """Base error for the idempotency repository."""


class IdempotencyConflictError(IdempotencyError):
    """The idempotency key was already used for a different request."""


@dataclass(frozen=True)
class InvoiceRequest:
    idempotency_key: str
    company: CompanyKey
    request_hash: str
    status: RequestStatus
    autocount_invoice_id: str | None
    autocount_invoice_number: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    # Transient result of begin(); never stored in invoice_requests.
    is_new: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresRequestRepository:
    """Postgres-backed storage for idempotent invoice issue requests.

    Same public contract as the SQLite ``RequestRepository`` so
    ``InvoiceService`` and every caller work unchanged; only the storage
    backend differs. One connection is opened per operation — acceptable at
    this project's request volume (mobile quick-entry, not bulk import) and
    avoids holding a pooled connection open across a serverless invocation.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._init_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _fetch(self, key: str) -> InvoiceRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoice_requests WHERE idempotency_key = %s", (key,)
            ).fetchone()
        return self._to_request(row) if row is not None else None

    def _to_request(self, row: dict[str, Any]) -> InvoiceRequest:
        try:
            company = CompanyKey(row["company"])
            status = RequestStatus(row["status"])
        except ValueError:
            raise IdempotencyError(
                "stored idempotency row has an unknown company or status"
            ) from None
        return InvoiceRequest(
            idempotency_key=row["idempotency_key"],
            company=company,
            request_hash=row["request_hash"],
            status=status,
            autocount_invoice_id=row["autocount_invoice_id"],
            autocount_invoice_number=row["autocount_invoice_number"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def begin(self, key: str, company: CompanyKey, request_hash: str) -> InvoiceRequest:
        """Insert a pending row, or return the stored row for the same request.

        Rejects with ``IdempotencyConflictError`` when the key was already
        used with a different request body or company. ``pending`` and
        ``ambiguous`` rows are returned without success data so the caller
        reconciles them before replaying.
        """
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO invoice_requests "
                "(idempotency_key, company, request_hash, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (key, company.value, request_hash, RequestStatus.PENDING.value, now, now),
            )
            inserted = cursor.rowcount == 1
            conn.commit()
        existing = self._fetch(key)
        if existing is None:
            raise IdempotencyError(f"idempotency row {key!r} could not be stored")
        if existing.request_hash != request_hash or existing.company is not company:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was already used for a different request"
            )
        return replace(existing, is_new=inserted)

    def get(self, key: str) -> InvoiceRequest | None:
        """Return the stored request for ``key``, or None."""
        return self._fetch(key)

    def record_price_overrides(
        self,
        key: str,
        autocount_invoice_id: str,
        overrides: tuple[dict[str, Any], ...],
    ) -> None:
        """Persist non-authoritative issued-vs-original price metadata."""
        if self._fetch(key) is None:
            raise IdempotencyError(f"no idempotency row for key {key!r}")
        with self._connect() as conn:
            for override in overrides:
                conn.execute(
                    "INSERT INTO invoice_price_overrides "
                    "(idempotency_key, autocount_invoice_id, item_id, "
                    "original_unit_price, issued_unit_price, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (idempotency_key, item_id) DO NOTHING",
                    (
                        key,
                        autocount_invoice_id,
                        override["item_id"],
                        str(override["original_unit_price"]),
                        str(override["issued_unit_price"]),
                        _now(),
                    ),
                )
            conn.commit()

    def list_price_overrides(self, key: str) -> list[dict[str, Any]]:
        """Return persisted price override metadata for one request."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, original_unit_price, issued_unit_price, "
                "autocount_invoice_id FROM invoice_price_overrides "
                "WHERE idempotency_key = %s ORDER BY id",
                (key,),
            ).fetchall()
        return [
            {
                "item_id": row["item_id"],
                "original_unit_price": Decimal(row["original_unit_price"]),
                "issued_unit_price": Decimal(row["issued_unit_price"]),
                "autocount_invoice_id": row["autocount_invoice_id"],
            }
            for row in rows
        ]

    def _transition(
        self,
        key: str,
        *,
        status: RequestStatus,
        invoice_id: str | None = None,
        invoice_number: str | None = None,
        error_message: str | None = None,
    ) -> InvoiceRequest:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE invoice_requests SET status = %s, autocount_invoice_id = %s, "
                "autocount_invoice_number = %s, error_message = %s, updated_at = %s "
                "WHERE idempotency_key = %s",
                (status.value, invoice_id, invoice_number, error_message, _now(), key),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise IdempotencyError(f"no idempotency row for key {key!r}")
            conn.commit()
        return self._fetch(key)

    def mark_succeeded(
        self, key: str, autocount_invoice_id: str, autocount_invoice_number: str
    ) -> InvoiceRequest:
        """Record that AutoCount created the invoice."""
        return self._transition(
            key,
            status=RequestStatus.SUCCEEDED,
            invoice_id=autocount_invoice_id,
            invoice_number=autocount_invoice_number,
        )

    def mark_ambiguous(self, key: str, message: str) -> InvoiceRequest:
        """Record that a write timed out and may or may not have been applied."""
        return self._transition(key, status=RequestStatus.AMBIGUOUS, error_message=message)

    def mark_failed(self, key: str, message: str) -> InvoiceRequest:
        """Record a definite failure before any AutoCount invoice was created."""
        return self._transition(key, status=RequestStatus.FAILED, error_message=message)
