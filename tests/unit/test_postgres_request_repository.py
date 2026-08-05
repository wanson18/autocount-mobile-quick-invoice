"""Postgres idempotency repository — same contract as the SQLite repository.

Runs against a real Postgres database supplied via ``POSTGRES_TEST_DSN``
(e.g. ``postgresql://user:pass@localhost:5432/autocount_test``), the same
principle as the SQLite tests exercising a real file rather than a mock
connection. Skipped entirely when no test database is configured — CI/local
runs stay green on SQLite alone; this suite is opt-in for anyone who wants to
verify the Postgres path before deploying it (e.g. against a local Postgres
container or a scratch Vercel Postgres database).

Each test truncates its own tables at the start so runs are independent and
repeatable against a shared test database.
"""

import os
from datetime import datetime

import pytest

from app.models.company import CompanyKey

psycopg = pytest.importorskip("psycopg")

POSTGRES_TEST_DSN = os.environ.get("POSTGRES_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DSN,
    reason="set POSTGRES_TEST_DSN to a scratch Postgres database to run these tests",
)

if POSTGRES_TEST_DSN:
    from app.repositories.postgres_request_repository import (
        IdempotencyConflictError,
        IdempotencyError,
        PostgresRequestRepository,
        RequestStatus,
    )

KEY = "draft-key-1"
HASH_A = "hash-a"
HASH_B = "hash-b"


@pytest.fixture
def repo():
    repository = PostgresRequestRepository(POSTGRES_TEST_DSN)
    with psycopg.connect(POSTGRES_TEST_DSN) as conn:
        conn.execute("TRUNCATE invoice_price_overrides, invoice_requests")
        conn.commit()
    return repository


def row_count(dsn):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT COUNT(*) FROM invoice_requests").fetchone()[0]


def test_begin_creates_pending_row(repo):
    request = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert request.is_new is True
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

    assert replayed.is_new is False
    assert replayed.status is RequestStatus.SUCCEEDED
    assert replayed.autocount_invoice_id == "inv-42"
    assert replayed.autocount_invoice_number == "INV-2026-0001"
    assert row_count(POSTGRES_TEST_DSN) == 1


def test_begin_with_different_request_is_rejected(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    with pytest.raises(IdempotencyConflictError):
        repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_B)
    assert row_count(POSTGRES_TEST_DSN) == 1


def test_same_key_with_different_company_is_rejected(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    with pytest.raises(IdempotencyConflictError):
        repo.begin(KEY, CompanyKey.SDN_BHD, HASH_A)
    assert row_count(POSTGRES_TEST_DSN) == 1


def test_pending_replay_returns_pending_without_success_data(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    replayed = repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    assert replayed.is_new is False
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


def test_record_and_list_price_overrides(repo):
    repo.begin(KEY, CompanyKey.ENTERPRISE, HASH_A)
    repo.mark_succeeded(KEY, "inv-1", "INV-1")
    repo.record_price_overrides(
        KEY,
        "inv-1",
        (
            {
                "item_id": "ITEM-1",
                "original_unit_price": "10.00",
                "issued_unit_price": "9.50",
            },
        ),
    )
    overrides = repo.list_price_overrides(KEY)
    assert len(overrides) == 1
    assert overrides[0]["item_id"] == "ITEM-1"
    assert str(overrides[0]["original_unit_price"]) == "10.00"
    assert str(overrides[0]["issued_unit_price"]) == "9.50"


def test_concurrent_begin_with_same_key_creates_single_row():
    import threading

    barrier = threading.Barrier(2)
    results = []

    with psycopg.connect(POSTGRES_TEST_DSN) as conn:
        conn.execute("TRUNCATE invoice_price_overrides, invoice_requests")
        conn.commit()

    def worker():
        repository = PostgresRequestRepository(POSTGRES_TEST_DSN)
        barrier.wait()
        results.append(repository.begin(KEY, CompanyKey.ENTERPRISE, HASH_A))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(r.idempotency_key == KEY for r in results)
    assert row_count(POSTGRES_TEST_DSN) == 1
