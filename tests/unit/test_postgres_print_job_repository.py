"""Postgres print-job repository — same contract as the SQLite repository.

Skipped unless ``POSTGRES_TEST_DSN`` is set, matching
``test_postgres_request_repository.py``.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.models.company import CompanyKey

psycopg = pytest.importorskip("psycopg")

POSTGRES_TEST_DSN = os.environ.get("POSTGRES_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DSN,
    reason="set POSTGRES_TEST_DSN to a scratch Postgres database to run these tests",
)

if POSTGRES_TEST_DSN:
    from app.repositories.postgres_print_job_repository import (
        PostgresPrintJobRepository,
    )
    from app.repositories.print_job_repository import (
        PrintJobNotFoundError,
        PrintJobStateError,
        PrintJobStatus,
    )

CLOUD_URL = "https://cloud.test.invalid/invoice?docKey=inv-1"
PRINTER = "EPSONE85FF0 (L6460 Series)"


@pytest.fixture
def repo():
    repository = PostgresPrintJobRepository(POSTGRES_TEST_DSN)
    with psycopg.connect(POSTGRES_TEST_DSN) as conn:
        conn.execute("TRUNCATE print_jobs")
        conn.commit()
    return repository


def _enqueue(repo, *, doc_no="INV-1", company=CompanyKey.SDN_BHD):
    return repo.enqueue(
        company=company,
        doc_no=doc_no,
        cloud_report_url=CLOUD_URL,
        printer_name=PRINTER,
    )


def test_enqueue_creates_queued_job(repo):
    job = _enqueue(repo)
    assert job.status is PrintJobStatus.QUEUED
    assert job.doc_no == "INV-1"
    assert job.cloud_report_url == CLOUD_URL


def test_claim_next_takes_oldest_queued_job(repo):
    first = _enqueue(repo, doc_no="INV-1")
    _enqueue(repo, doc_no="INV-2")
    claimed = repo.claim_next()
    assert claimed.id == first.id
    assert claimed.status is PrintJobStatus.PRINTING


def test_claim_next_returns_none_when_queue_is_empty(repo):
    assert repo.claim_next() is None


def test_mark_printed_from_printing(repo):
    job = _enqueue(repo)
    repo.claim_next()
    assert repo.mark_printed(job.id).status is PrintJobStatus.PRINTED


def test_cannot_mark_printed_from_queued(repo):
    job = _enqueue(repo)
    with pytest.raises(PrintJobStateError):
        repo.mark_printed(job.id)


def test_complete_unknown_job_raises(repo):
    with pytest.raises(PrintJobNotFoundError):
        repo.mark_printed("no-such-job")


def test_stale_printing_job_is_reclaimed(repo):
    job = _enqueue(repo)
    repo.claim_next()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    with psycopg.connect(POSTGRES_TEST_DSN) as conn:
        conn.execute(
            "UPDATE print_jobs SET claimed_at = %s WHERE id = %s",
            (stale, job.id),
        )
        conn.commit()
    reclaimed = repo.claim_next(lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.status is PrintJobStatus.PRINTING


def test_public_view_omits_cloud_url(repo):
    public = _enqueue(repo).public_dict()
    assert "cloud_report_url" not in public
    assert CLOUD_URL not in str(public)
    assert PRINTER not in str(public)
