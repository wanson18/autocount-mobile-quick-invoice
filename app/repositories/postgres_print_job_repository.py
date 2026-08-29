"""Postgres-backed queue of office print jobs.

Same contract as ``app.repositories.print_job_repository.PrintJobRepository``
(SQLite), reimplemented against Postgres so jobs survive a serverless
platform's ephemeral filesystem (e.g. Vercel). Claim uses
``SELECT ... FOR UPDATE SKIP LOCKED`` so two agents cannot take the same job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.models.company import CompanyKey
from app.repositories.print_job_repository import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    PrintJob,
    PrintJobError,
    PrintJobNotFoundError,
    PrintJobStateError,
    PrintJobStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_jobs (
  id TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  status TEXT NOT NULL,
  cloud_report_url TEXT NOT NULL,
  printer_name TEXT NOT NULL,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  claimed_at TEXT
);

CREATE INDEX IF NOT EXISTS print_jobs_status_created
  ON print_jobs (status, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresPrintJobRepository:
    """Postgres-backed storage for office print jobs."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._init_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def _to_job(self, row: dict[str, Any]) -> PrintJob:
        try:
            company = CompanyKey(row["company"])
            status = PrintJobStatus(row["status"])
        except ValueError:
            raise PrintJobError(
                "stored print job has an unknown company or status"
            ) from None
        return PrintJob(
            id=row["id"],
            company=company,
            doc_no=row["doc_no"],
            status=status,
            cloud_report_url=row["cloud_report_url"],
            printer_name=row["printer_name"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            claimed_at=row["claimed_at"],
        )

    def enqueue(
        self,
        *,
        company: CompanyKey,
        doc_no: str,
        cloud_report_url: str,
        printer_name: str,
        job_id: str | None = None,
    ) -> PrintJob:
        now = _now()
        job_id = job_id or str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO print_jobs "
                "(id, company, doc_no, status, cloud_report_url, printer_name, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    company.value,
                    doc_no,
                    PrintJobStatus.QUEUED.value,
                    cloud_report_url,
                    printer_name,
                    now,
                    now,
                ),
            )
            conn.commit()
        job = self.get(job_id)
        if job is None:
            raise PrintJobError(f"print job {job_id!r} could not be stored")
        return job

    def get(self, job_id: str) -> PrintJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM print_jobs WHERE id = %s", (job_id,)
            ).fetchone()
        return self._to_job(row) if row is not None else None

    def claim_next(
        self, *, lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS
    ) -> PrintJob | None:
        now = _now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE print_jobs SET status = %s, claimed_at = NULL, "
                "updated_at = %s WHERE status = %s AND claimed_at IS NOT NULL "
                "AND claimed_at < %s",
                (
                    PrintJobStatus.QUEUED.value,
                    now,
                    PrintJobStatus.PRINTING.value,
                    stale_before,
                ),
            )
            row = conn.execute(
                "UPDATE print_jobs SET status = %s, claimed_at = %s, updated_at = %s "
                "WHERE id = ("
                "  SELECT id FROM print_jobs WHERE status = %s "
                "  ORDER BY created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1"
                ") RETURNING *",
                (
                    PrintJobStatus.PRINTING.value,
                    now,
                    now,
                    PrintJobStatus.QUEUED.value,
                ),
            ).fetchone()
            conn.commit()
        return self._to_job(row) if row is not None else None

    def mark_printed(self, job_id: str) -> PrintJob:
        return self._complete(job_id, PrintJobStatus.PRINTED, error_message=None)

    def mark_failed(self, job_id: str, message: str) -> PrintJob:
        return self._complete(
            job_id, PrintJobStatus.FAILED, error_message=message
        )

    def _complete(
        self,
        job_id: str,
        status: PrintJobStatus,
        *,
        error_message: str | None,
    ) -> PrintJob:
        job = self.get(job_id)
        if job is None:
            raise PrintJobNotFoundError(f"no print job {job_id!r}")
        if job.status is not PrintJobStatus.PRINTING:
            raise PrintJobStateError(
                f"print job {job_id!r} is {job.status.value}, not printing"
            )
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE print_jobs SET status = %s, error_message = %s, "
                "updated_at = %s WHERE id = %s",
                (status.value, error_message, now, job_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise PrintJobNotFoundError(f"no print job {job_id!r}")
            conn.commit()
        completed = self.get(job_id)
        if completed is None:
            raise PrintJobError(f"print job {job_id!r} disappeared during complete")
        return completed
