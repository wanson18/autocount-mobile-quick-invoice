"""Application dependency wiring for the private single-tenant deployment.

Constructs the AutoCount client, master-data adapter, idempotency repository,
and invoice service lazily from environment variables and exposes them as
FastAPI dependencies. Credentials and the account-book IDs exist only
server-side; the client never accepts them from a request.

Lazy singletons so importing this module (and ``app.main``) never fails on a
missing environment: construction errors surface on first request, and tests
can override the dependency functions with fakes.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.autocount import AutoCountClient
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.config import get_company
from app.repositories.request_repository import RequestRepository
from app.services.invoice_service import InvoiceService

ENV_KEY_ID = "AUTOCOUNT_API_KEY_ID"
ENV_API_KEY = "AUTOCOUNT_API_KEY"
ENV_DB_PATH = "INVOICE_REQUESTS_DB"
DEFAULT_DB_PATH = "data/invoice_requests.db"

_client_instance: AutoCountClient | None = None
_master_data_instance: AutoCountMasterDataAdapter | None = None
_repository_instance: RequestRepository | None = None
_service_instance: InvoiceService | None = None


def _get_client() -> AutoCountClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AutoCountClient(
            os.environ.get(ENV_KEY_ID, ""),
            os.environ.get(ENV_API_KEY, ""),
        )
    return _client_instance


def get_master_data() -> AutoCountMasterDataAdapter:
    global _master_data_instance
    if _master_data_instance is None:
        _master_data_instance = AutoCountMasterDataAdapter(_get_client())
    return _master_data_instance


def _get_repository() -> RequestRepository:
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = RequestRepository(
            Path(os.environ.get(ENV_DB_PATH, DEFAULT_DB_PATH))
        )
    return _repository_instance


def get_invoice_service() -> InvoiceService:
    global _service_instance
    if _service_instance is None:
        _service_instance = InvoiceService(
            company_resolver=get_company,
            master_data=get_master_data(),
            client=_get_client(),
            requests=_get_repository(),
        )
    return _service_instance
