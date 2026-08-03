"""Task 5 — Idempotency repository for invoice requests.

Rules proven here, against a real SQLite file:

- Same key plus same request returns the stored result.
- Same key plus different request is rejected.
- Pending and ambiguous requests are returned with their status and never
  with success data, so the invoice service reconciles them before replay.
- A concurrent double-tap cannot create two rows.
- The stored table matches the implementation plan's schema.
"""

import sqlite3
import threading
from datetime import datetime

import pytest

from app.models.company import CompanyKey
from app.repositories.request_repository import (
    IdempotencyConflictError,
    IdempotencyError,
    InvoiceRequest,
    RequestRepository,
    RequestStatus,
)

KEY = "draft-key-1"
HASH_A = "hash-a"
HASH_B = "hash-b"


@pytest.fixture
def repo(tmp_path):
    return RequestRepository(tmp_path / "requests.db")


def row_count(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM invoice_requests").fetchone()[0]


def test_begin_creates_pending_row(repo):
    request = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert request.idempotency_key == KEY
    assert request.company is CompanyKey.ENTERPRISE
    assert request.request_hash == HASH_A
    assert request.status is RequestStatus.PENDING
    assert request.autocount_invoice_id is None
    assert request.autocount_invoice_number is None
    assert request.error_message is None


def test_begin_returns_stored_result_for_same_key_and_request(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    repo.mark_succeeded(KEY, "inv-42", "INV-2026-0001")

    replayed = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)

    assert replayed.status is RequestStatus.SUCCEEDED
    assert replayed.autocount_invoice_id == "inv-42"
    assert replayed.autocount_invoice_number == "INV-2026-0001"
    assert row_count(repo._db_path) == 1


def test_begin_with_different_request_is_rejected(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    with pytest.raises(IdempotencyConflictError):
        repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_B)
    assert row_count(repo._db_path) == 1


def test_same_key_with_different_company_is_rejected(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    with pytest.raises(IdempotencyConflictError):
        repo.begin(KEY, CompanyKey.SDN_BHD, HASH_A)
    assert row_count(repo._db_path) == 1


def test_pending_replay_returns_pending_without_success_data(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    replayed = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert replayed.status is RequestStatus.PENDING
    assert replayed.autocount_invoice_id is None
    assert replayed.autocount_invoice_number is None


def test_ambiguous_replay_returns_ambiguous_without_success_data(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    repo.mark_ambiguous(KEY, "write timed out; may or may not have been applied")
    replayed = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert replayed.status is RequestStatus.AMBIGUOUS
    assert replayed.autocount_invoice_id is None
    assert replayed.autocount_invoice_number is None
    assert "timed out" in replayed.error_message


def test_failed_replay_returns_stored_failure(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    repo.mark_failed(KEY, "AutoCount rejected the request (status 400)")
    replayed = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert replayed.status is RequestStatus.FAILED
    assert replayed.error_message == "AutoCount rejected the request (status 400)"


def test_mark_succeeded_stores_autocount_identity(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    request = repo.mark_succeeded(KEY, "inv-7", "INV-9")
    assert request.status is RequestStatus.SUCCEEDED
    assert request.autocount_invoice_id == "inv-7"
    assert request.autocount_invoice_number == "INV-9"
    assert request.error_message is None


def test_mark_failed_stores_error_message(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    request = repo.mark_failed(KEY, "invalid company")
    assert request.status is RequestStatus.FAILED
    assert request.error_message == "invalid company"
    assert request.autocount_invoice_id is None


def test_mark_ambiguous_stores_message(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    request = repo.mark_ambiguous(KEY, "write timed out")
    assert request.status is RequestStatus.AMBIGUOUS
    assert request.error_message == "write timed out"


def test_updated_at_advances_on_transition(repo):
    created = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert created.updated_at == created.created_at
    assert datetime.fromisoformat(created.created_at) is not None
    succeeded = repo.mark_succeeded(KEY, "inv-1", "INV-1")
    assert succeeded.updated_at > created.updated_at


def test_get_returns_none_for_unknown_key(repo):
    assert repo.get("no-such-key") is None


def test_get_returns_stored_request(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    request = repo.get(KEY)
    assert request.idempotency_key == KEY
    assert request.company is CompanyKey.ENTERPRISE


def test_mark_on_unknown_key_raises(repo):
    with pytest.raises(IdempotencyError):
        repo.mark_succeeded("no-such-key", "inv-1", "INV-1")


def test_repository_creates_missing_directories(tmp_path):
    repo = RequestRepository(tmp_path / "nested" / "data" / "requests.db")
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert (tmp_path / "nested" / "data" / "requests.db").exists()


def test_table_matches_plan_schema(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    with sqlite3.connect(repo._db_path) as conn:
        columns = conn.execute("PRAGMA table_info(invoice_requests)").fetchall()
    assert [(c[1], c[2], c[3], c[5]) for c in columns] == [
        ("idempotency_key", "TEXT", 0, 1),
        ("company", "TEXT", 1, 0),
        ("request_hash", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("autocount_invoice_id", "TEXT", 0, 0),
        ("autocount_invoice_number", "TEXT", 0, 0),
        ("error_message", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ]


def test_concurrent_begin_with_same_key_creates_single_row(tmp_path):
    db_path = tmp_path / "requests.db"
    barrier = threading.Barrier(2)
    results = []

    def worker():
        repo = RequestRepository(db_path)
        barrier.wait()
        results.append(repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(r.idempotency_key == KEY for r in results)
    assert row_count(db_path) == 1
