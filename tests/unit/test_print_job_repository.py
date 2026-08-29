"""Print-job repository: enqueue, claim, and the queued→printing→printed|failed machine.

Rules proven here against a real SQLite file:

- Enqueue stores a queued job with the resolved Cloud URL (agent-only).
- Claim takes the oldest queued job and marks it printing.
- Two concurrent claims cannot take the same job.
- Terminal transitions (printed / failed) are only legal from printing.
- A stale printing job is reclaimed so a crashed agent cannot block the queue.
- Public mobile fields never include the Cloud URL or printer name.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.models.company import CompanyKey
from app.repositories.print_job_repository import (
    PrintJobNotFoundError,
    PrintJobRepository,
    PrintJobStateError,
    PrintJobStatus,
)

CLOUD_URL = "https://cloud.test.invalid/invoice?docKey=inv-1"
PRINTER = "EPSONE85FF0 (L6460 Series)"


@pytest.fixture
def repo(tmp_path):
    return PrintJobRepository(tmp_path / "print_jobs.db")


def _enqueue(repo, *, doc_no="INV-1", company=CompanyKey.SDN_BHD, **kwargs):
    return repo.enqueue(
        company=company,
        doc_no=doc_no,
        cloud_report_url=CLOUD_URL,
        printer_name=PRINTER,
        **kwargs,
    )


def test_enqueue_creates_queued_job(repo):
    job = _enqueue(repo)
    assert job.status is PrintJobStatus.QUEUED
    assert job.company is CompanyKey.SDN_BHD
    assert job.doc_no == "INV-1"
    assert job.cloud_report_url == CLOUD_URL
    assert job.printer_name == PRINTER
    assert job.error_message is None
    assert job.claimed_at is None
    assert job.id


def test_claim_next_takes_oldest_queued_job(repo):
    first = _enqueue(repo, doc_no="INV-1")
    second = _enqueue(repo, doc_no="INV-2")

    claimed = repo.claim_next()

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status is PrintJobStatus.PRINTING
    assert claimed.claimed_at is not None
    assert repo.get(second.id).status is PrintJobStatus.QUEUED


def test_claim_next_returns_none_when_queue_is_empty(repo):
    assert repo.claim_next() is None


def test_mark_printed_from_printing(repo):
    job = _enqueue(repo)
    repo.claim_next()
    done = repo.mark_printed(job.id)
    assert done.status is PrintJobStatus.PRINTED
    assert done.error_message is None


def test_mark_failed_from_printing_stores_message(repo):
    job = _enqueue(repo)
    repo.claim_next()
    failed = repo.mark_failed(job.id, "Edge is not logged into AutoCount Cloud")
    assert failed.status is PrintJobStatus.FAILED
    assert "logged into AutoCount Cloud" in failed.error_message


def test_cannot_mark_printed_from_queued(repo):
    job = _enqueue(repo)
    with pytest.raises(PrintJobStateError, match="printing"):
        repo.mark_printed(job.id)
    assert repo.get(job.id).status is PrintJobStatus.QUEUED


def test_cannot_complete_a_printed_job(repo):
    job = _enqueue(repo)
    repo.claim_next()
    repo.mark_printed(job.id)
    with pytest.raises(PrintJobStateError, match="printed"):
        repo.mark_failed(job.id, "too late")
    with pytest.raises(PrintJobStateError, match="printed"):
        repo.mark_printed(job.id)


def test_complete_unknown_job_raises(repo):
    with pytest.raises(PrintJobNotFoundError):
        repo.mark_printed("no-such-job")


def test_stale_printing_job_is_reclaimed(repo):
    job = _enqueue(repo)
    stale_claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    repo.claim_next()
    # Force the claim timestamp into the past so the next claim treats it as stale.
    with sqlite3.connect(repo._db_path) as conn:
        conn.execute(
            "UPDATE print_jobs SET claimed_at = ? WHERE id = ?",
            (stale_claimed_at, job.id),
        )

    reclaimed = repo.claim_next(lease_seconds=60)

    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.status is PrintJobStatus.PRINTING


def test_fresh_printing_job_is_not_reclaimed(repo):
    first = _enqueue(repo, doc_no="INV-1")
    _enqueue(repo, doc_no="INV-2")
    claimed = repo.claim_next(lease_seconds=600)
    assert claimed.id == first.id

    next_job = repo.claim_next(lease_seconds=600)
    assert next_job.doc_no == "INV-2"
    assert repo.get(first.id).status is PrintJobStatus.PRINTING


def test_public_view_omits_cloud_url_and_printer(repo):
    job = _enqueue(repo)
    public = job.public_dict()
    assert public["job_id"] == job.id
    assert public["status"] == "queued"
    assert public["doc_no"] == "INV-1"
    assert public["company"] == "sdn_bhd"
    assert "cloud_report_url" not in public
    assert "cloud" not in str(public).lower()
    assert "printer" not in str(public).lower()
    assert CLOUD_URL not in str(public)
    assert PRINTER not in str(public)


def test_agent_view_includes_cloud_url_and_printer(repo):
    job = _enqueue(repo)
    agent = job.agent_dict()
    assert agent["cloud_report_url"] == CLOUD_URL
    assert agent["printer_name"] == PRINTER
    assert agent["job_id"] == job.id
    assert agent["doc_no"] == "INV-1"


def test_get_returns_none_for_unknown_id(repo):
    assert repo.get("no-such-job") is None


def test_concurrent_claim_gives_each_job_to_one_agent(tmp_path):
    db_path = tmp_path / "print_jobs.db"
    setup = PrintJobRepository(db_path)
    first = _enqueue(setup, doc_no="INV-1")
    second = _enqueue(setup, doc_no="INV-2")
    barrier = threading.Barrier(2)
    results = []

    def worker():
        repo = PrintJobRepository(db_path)
        barrier.wait()
        results.append(repo.claim_next())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed_ids = {job.id for job in results if job is not None}
    assert claimed_ids == {first.id, second.id}
    with sqlite3.connect(db_path) as conn:
        statuses = {row[0] for row in conn.execute("SELECT status FROM print_jobs")}
    assert statuses == {"printing"}
