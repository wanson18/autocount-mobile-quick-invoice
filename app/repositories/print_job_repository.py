"""SQLite-backed queue of office print jobs.

A mobile client enqueues by company + document number. The Windows print
agent claims the oldest queued job, prints the official AutoCount Cloud
report, then reports printed or failed. This table is print-job metadata
only — never a ledger, never authoritative for accounting data.

One connection is opened per operation. Claim uses ``BEGIN IMMEDIATE`` so
two agents polling at once cannot take the same job.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app.models.company import CompanyKey

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

#: How long a claimed job may stay in ``printing`` before another agent may
#: take it. Long enough for Edge to render the Cloud report; short enough
#: that a crashed agent does not block the office printer.
DEFAULT_CLAIM_LEASE_SECONDS = 600


class PrintJobStatus(str, Enum):
    QUEUED = "queued"
    PRINTING = "printing"
    PRINTED = "printed"
    FAILED = "failed"


class PrintJobError(Exception):
    """Base error for the print-job repository."""


class PrintJobNotFoundError(PrintJobError):
    """No print job exists for the requested id."""


class PrintJobStateError(PrintJobError):
    """The requested transition is illegal for the job's current status."""


@dataclass(frozen=True)
class PrintJob:
    id: str
    company: CompanyKey
    doc_no: str
    status: PrintJobStatus
    cloud_report_url: str
    printer_name: str
    error_message: str | None
    created_at: str
    updated_at: str
    claimed_at: str | None

    def public_dict(self) -> dict[str, Any]:
        """Mobile-safe view: identity and status only.

        The Cloud report URL embeds the server-side account-book path and must
        never be sent to the iPhone. The office printer name is an agent
        concern, not a browser one.
        """
        return {
            "job_id": self.id,
            "company": self.company.value,
            "doc_no": self.doc_no,
            "status": self.status.value,
            "error_message": self.error_message,
        }

    def agent_dict(self) -> dict[str, Any]:
        """Trusted-agent view, including the resolved Cloud report URL."""
        return {
            "job_id": self.id,
            "company": self.company.value,
            "doc_no": self.doc_no,
            "status": self.status.value,
            "cloud_report_url": self.cloud_report_url,
            "printer_name": self.printer_name,
            "error_message": self.error_message,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PrintJobRepository:
    """SQLite-backed storage for office print jobs."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self, *, immediate: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        if immediate:
            # Autocommit mode so claim_next can issue BEGIN IMMEDIATE itself
            # rather than nesting inside sqlite3's implicit DEFERRED transaction.
            conn.isolation_level = None
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _to_job(self, row: sqlite3.Row) -> PrintJob:
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
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        job = self.get(job_id)
        if job is None:
            raise PrintJobError(f"print job {job_id!r} could not be stored")
        return job

    def get(self, job_id: str) -> PrintJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM print_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._to_job(row) if row is not None else None

    def claim_next(
        self, *, lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS
    ) -> PrintJob | None:
        """Atomically take the oldest queued job, reclaiming stale claims first."""
        now = _now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat()
        with self._connect(immediate=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE print_jobs SET status = ?, claimed_at = NULL, "
                "updated_at = ? WHERE status = ? AND claimed_at IS NOT NULL "
                "AND claimed_at < ?",
                (
                    PrintJobStatus.QUEUED.value,
                    now,
                    PrintJobStatus.PRINTING.value,
                    stale_before,
                ),
            )
            row = conn.execute(
                "SELECT * FROM print_jobs WHERE status = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (PrintJobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE print_jobs SET status = ?, claimed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (PrintJobStatus.PRINTING.value, now, now, row["id"]),
            )
            job_id = row["id"]
            conn.execute("COMMIT")
        return self.get(job_id)

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
            conn.execute(
                "UPDATE print_jobs SET status = ?, error_message = ?, "
                "updated_at = ? WHERE id = ?",
                (status.value, error_message, now, job_id),
            )
        completed = self.get(job_id)
        if completed is None:
            raise PrintJobError(f"print job {job_id!r} disappeared during complete")
        return completed
