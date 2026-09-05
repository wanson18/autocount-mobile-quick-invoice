"""Shared idempotency contract and the SQLite implementation.

Backs the idempotent issue flow with a SQLite ``invoice_requests`` table as
specified in the implementation plan. This module also owns the contract both
storage backends share — status enum, exceptions, request record, row
mapping, and the ``RequestRepositoryPort`` protocol — so ``InvoiceService``
and the FastAPI error handlers compare against exactly one set of symbols
regardless of which backend ``app.dependencies`` wires in. The Postgres
implementation (``app.repositories.postgres_request_repository``) imports
these instead of defining its own, which is what makes a replay and a
conflict behave identically on either backend.

Rules:

- Same idempotency key plus the same request body returns the stored result.
- Same idempotency key with a different request body is rejected with
  ``IdempotencyConflictError`` before any AutoCount call is made.
- Rows are inserted as ``pending``. A later ``succeeded`` row is the
  authoritative result; ``pending`` and ``ambiguous`` rows are returned as-is
  so the invoice service reconciles them (Task 8) before replaying.
- Price overrides are stored separately as non-authoritative audit metadata.

One connection is opened per operation: SQLite serialises writers with file
locking, so a double-tap from the mobile client cannot create two rows, and a
single repository instance is safe to share across threads.

This is request metadata only, never a ledger: it is never authoritative for
accounting data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_request(row: Any) -> InvoiceRequest:
    """Build the shared record from one stored row (SQLite or Postgres).

    Works with anything supporting ``row["column"]`` subscripting, which
    covers both ``sqlite3.Row`` and psycopg's ``dict_row``. An unknown company
    or status fails closed rather than producing a record the service cannot
    interpret.
    """
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


class RequestRepositoryPort(Protocol):
    """The storage contract ``InvoiceService`` depends on.

    Both the SQLite and Postgres repositories satisfy this structurally; the
    service and ``app.dependencies`` accept either one.
    """

    def begin(self, key: str, company: CompanyKey, request_hash: str) -> InvoiceRequest:
        """Insert a pending row, or return the stored row for the same request."""
        ...

    def get(self, key: str) -> InvoiceRequest | None:
        """Return the stored request for ``key``, or None."""
        ...

    def record_price_overrides(
        self,
        key: str,
        autocount_invoice_id: str,
        overrides: tuple[dict[str, Any], ...],
    ) -> None:
        """Persist non-authoritative issued-vs-original price metadata."""
        ...

    def list_price_overrides(self, key: str) -> list[dict[str, Any]]:
        """Return persisted price override metadata for one request."""
        ...

    def mark_succeeded(
        self, key: str, autocount_invoice_id: str, autocount_invoice_number: str
    ) -> InvoiceRequest:
        """Record that AutoCount created the invoice."""
        ...

    def mark_ambiguous(self, key: str, message: str) -> InvoiceRequest:
        """Record that a write timed out and may or may not have been applied."""
        ...

    def mark_failed(self, key: str, message: str) -> InvoiceRequest:
        """Record a definite failure before any AutoCount invoice was created."""
        ...


class RequestRepository:
    """SQLite-backed storage for idempotent invoice issue requests."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _fetch(self, key: str) -> InvoiceRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoice_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return to_request(row) if row is not None else None

    def begin(self, key: str, company: CompanyKey, request_hash: str) -> InvoiceRequest:
        """Insert a pending row, or return the stored row for the same request.

        Rejects with ``IdempotencyConflictError`` when the key was already
        used with a different request body or company. ``pending`` and
        ``ambiguous`` rows are returned without success data so the caller
        reconciles them before replaying.
        """
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO invoice_requests "
                "(idempotency_key, company, request_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, company.value, request_hash, RequestStatus.PENDING.value, now, now),
            )
            inserted = cursor.rowcount == 1
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
                    "INSERT OR IGNORE INTO invoice_price_overrides "
                    "(idempotency_key, autocount_invoice_id, item_id, "
                    "original_unit_price, issued_unit_price, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        autocount_invoice_id,
                        override["item_id"],
                        str(override["original_unit_price"]),
                        str(override["issued_unit_price"]),
                        utc_now_iso(),
                    ),
                )

    def list_price_overrides(self, key: str) -> list[dict[str, Any]]:
        """Return persisted price override metadata for one request."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, original_unit_price, issued_unit_price, "
                "autocount_invoice_id FROM invoice_price_overrides "
                "WHERE idempotency_key = ? ORDER BY id",
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
                "UPDATE invoice_requests SET status = ?, autocount_invoice_id = ?, "
                "autocount_invoice_number = ?, error_message = ?, updated_at = ? "
                "WHERE idempotency_key = ?",
                (status.value, invoice_id, invoice_number, error_message, utc_now_iso(), key),
            )
            if cursor.rowcount == 0:
                raise IdempotencyError(f"no idempotency row for key {key!r}")
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
