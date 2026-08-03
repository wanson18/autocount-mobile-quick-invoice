"""Idempotency repository for invoice issue requests.

Backs the idempotent issue flow with a SQLite ``invoice_requests`` table as
specified in the implementation plan. Rules:

- Same idempotency key plus the same request body returns the stored result.
- Same idempotency key with a different request body is rejected with
  ``IdempotencyConflictError`` before any AutoCount call is made.
- Rows are inserted as ``pending``. A later ``succeeded`` row is the
  authoritative result; ``pending`` and ``ambiguous`` rows are returned as-is
  so the invoice service reconciles them (Task 8) before replaying.

One connection is opened per operation: SQLite serialises writers with file
locking, so a double-tap from the mobile client cannot create two rows, and a
single repository instance is safe to share across threads.

This is request metadata only, never a ledger: it is never authoritative for
accounting data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            conn.execute(_SCHEMA)

    def _fetch(self, key: str) -> InvoiceRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoice_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._to_request(row) if row is not None else None

    def _to_request(self, row: sqlite3.Row) -> InvoiceRequest:
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
            conn.execute(
                "INSERT OR IGNORE INTO invoice_requests "
                "(idempotency_key, company, request_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, company.value, request_hash, RequestStatus.PENDING.value, now, now),
            )
        existing = self._fetch(key)
        if existing is None:
            raise IdempotencyError(f"idempotency row {key!r} could not be stored")
        if existing.request_hash != request_hash or existing.company is not company:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was already used for a different request"
            )
        return existing

    def get(self, key: str) -> InvoiceRequest | None:
        """Return the stored request for ``key``, or None."""
        return self._fetch(key)

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
                (status.value, invoice_id, invoice_number, error_message, _now(), key),
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
