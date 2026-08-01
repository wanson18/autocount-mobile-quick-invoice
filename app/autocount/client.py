"""Asynchronous transport boundary for the AutoCount Cloud Accounting API.

Protocol, per the official Cloud Accounting Integration API documentation:

- Base URL: ``https://accounting-api.autocountcloud.com/``.
- Every request carries the mandatory ``API-Key`` and ``Key-ID`` headers,
  supplied server-side at construction and never accepted from callers.
- Every endpoint is routed under ``/{accountBookId}/...`` where the account
  book ID comes only from the server-side ``CompanyConfig`` returned by
  ``app.config.get_company``.

Read-like and write-like calls are distinguished explicitly through the
``operation`` marker because AutoCount uses POST for listing (read) calls, so
the HTTP method alone cannot tell them apart. A timeout on an explicitly
marked write is ambiguous (AutoCount may have applied it) and raises
``AutoCountAmbiguousWriteError``; a timeout on a read is an ordinary transport
failure (``AutoCountTransportError``).

This module is deliberately generic: no master-data methods, invoice payload
mapping, retries, reconciliation, e-Invoice, PDF retrieval, FastAPI routes, or
logging. No automatic retry of any kind is configured.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, TypeAlias

import httpx

from app.autocount.errors import (
    AutoCountAmbiguousWriteError,
    AutoCountConfigError,
    AutoCountEndpointError,
    AutoCountRejectedError,
    AutoCountTransportError,
)
from app.config import CompanyConfig

DEFAULT_BASE_URL = "https://accounting-api.autocountcloud.com/"
DEFAULT_TIMEOUT = httpx.Timeout(30.0)
_REDACTED = "<redacted>"
_MAX_MESSAGE_LENGTH = 500
_MESSAGE_KEYS = (
    "message",
    "Message",
    "error",
    "Error",
    "errorMessage",
    "ErrorMessage",
    "errMsg",
    "detail",
)
_SAFE_ACCOUNT_BOOK_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

Transport: TypeAlias = "httpx.AsyncBaseTransport | None"


class RequestOperation(Enum):
    """Explicit semantic marker distinguishing read-like from write calls."""

    READ = "read"
    WRITE = "write"


def _extract_message(response: httpx.Response) -> str:
    """Best-effort AutoCount error message, untruncated.

    Length capping is applied only after redaction so a credential straddling
    the cap cannot leak a prefix (see ``AutoCountClient._rejection_message``).
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        for key in _MESSAGE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalise_timeout(timeout: httpx.Timeout | float | None) -> httpx.Timeout:
    """Return a validated, finite ``httpx.Timeout`` for the given input.

    ``None`` means the safe finite default. A bare number becomes a uniform
    timeout. ``httpx.Timeout`` instances are accepted only when every one of
    their components is present, numeric, finite, and greater than zero.
    """
    if timeout is None:
        return DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float, httpx.Timeout)):
        raise AutoCountConfigError(
            "timeout must be a finite positive number, an httpx.Timeout, or None"
        )
    if isinstance(timeout, httpx.Timeout):
        for name in ("connect", "read", "write", "pool"):
            _validate_timeout_component(getattr(timeout, name), name)
        return timeout
    _validate_timeout_component(timeout, "timeout")
    return httpx.Timeout(timeout)


def _validate_timeout_component(component: object, name: str) -> None:
    if component is None:
        raise AutoCountConfigError(f"timeout.{name} must not be disabled (None)")
    if isinstance(component, bool) or not isinstance(component, (int, float)):
        raise AutoCountConfigError(f"timeout.{name} must be a number")
    if not math.isfinite(component):
        raise AutoCountConfigError(f"timeout.{name} must be finite")
    if component <= 0:
        raise AutoCountConfigError(f"timeout.{name} must be greater than zero")


class AutoCountClient:
    """Async client for the AutoCount Cloud Accounting Integration API.

    Owns an ``httpx.AsyncClient`` lifecycle; use it with ``async with`` or
    close it explicitly with ``await client.aclose()``.
    """

    def __init__(
        self,
        key_id: str,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout | float | None = None,
        transport: Transport = None,
    ) -> None:
        key_id = key_id.strip() if isinstance(key_id, str) else ""
        api_key = api_key.strip() if isinstance(api_key, str) else ""
        if not key_id or not api_key:
            raise AutoCountConfigError("Key ID and API key must be non-empty")
        self._key_id = key_id
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self._timeout = _normalise_timeout(timeout)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=transport,
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AutoCountClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def read(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Run a read-like AutoCount call (may be a POST listing)."""
        return await self.request(
            company,
            method,
            endpoint,
            operation=RequestOperation.READ,
            params=params,
            json=json,
        )

    async def write(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Run a write call; a timeout here is ambiguous by definition."""
        return await self.request(
            company,
            method,
            endpoint,
            operation=RequestOperation.WRITE,
            params=params,
            json=json,
        )

    async def request(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        operation: RequestOperation,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Send one AutoCount request and normalise the outcome.

        The account-book ID is taken from ``company.account_book_id`` as the
        first path segment. Credentials are attached internally and can never
        be overridden by the caller.
        """
        if not isinstance(operation, RequestOperation):
            raise AutoCountConfigError("operation must be a RequestOperation")
        path = self._build_path(company, endpoint)
        headers = {"Key-ID": self._key_id, "API-Key": self._api_key}
        try:
            response = await self._client.request(
                method, path, headers=headers, params=params, json=json
            )
        except httpx.TimeoutException:
            if operation is RequestOperation.WRITE:
                raise AutoCountAmbiguousWriteError(
                    "AutoCount write request timed out; it may or may not have been applied"
                ) from None
            raise AutoCountTransportError(
                "AutoCount request timed out before an HTTP response was received"
            ) from None
        except httpx.RequestError:
            raise AutoCountTransportError(
                "AutoCount request failed before an HTTP response was received"
            ) from None
        if not 200 <= response.status_code < 300:
            raise AutoCountRejectedError(
                response.status_code, self._rejection_message(response, company)
            )
        return response

    def _build_path(self, company: CompanyConfig, endpoint: str) -> str:
        if not isinstance(company, CompanyConfig):
            raise AutoCountConfigError("company must be a server-side CompanyConfig")
        self._validate_endpoint(endpoint)
        account_book_id = company.account_book_id
        self._validate_account_book_id(account_book_id)
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return f"/{account_book_id}{endpoint}"

    @staticmethod
    def _validate_account_book_id(account_book_id: object) -> None:
        if not isinstance(account_book_id, str) or not account_book_id:
            raise AutoCountConfigError("company configuration is missing its account book ID")
        if not all(ch in _SAFE_ACCOUNT_BOOK_CHARS for ch in account_book_id):
            raise AutoCountConfigError(
                "company account book ID must contain only letters, digits, hyphens, and underscores"
            )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise AutoCountEndpointError("endpoint must be a non-empty string")
        if "://" in endpoint or endpoint.startswith("//"):
            raise AutoCountEndpointError(
                "endpoint must be relative, not an absolute or scheme-relative URL"
            )
        if "\\" in endpoint:
            raise AutoCountEndpointError("endpoint must not contain backslashes")
        if "?" in endpoint:
            raise AutoCountEndpointError("endpoint must not embed a query string; use params=")
        if "#" in endpoint:
            raise AutoCountEndpointError("endpoint must not contain a URL fragment")
        if any(segment in ("..", ".") for segment in endpoint.split("/")):
            raise AutoCountEndpointError("endpoint must not contain path traversal")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == "%" or ch.isspace() for ch in endpoint):
            raise AutoCountEndpointError(
                "endpoint must not contain percent escapes, control, or whitespace characters"
            )

    def _rejection_message(self, response: httpx.Response, company: CompanyConfig) -> str:
        message = self._redact(_extract_message(response), extra=(company.account_book_id,))
        if not message:
            message = f"AutoCount rejected the request (status {response.status_code})"
        return message[:_MAX_MESSAGE_LENGTH]

    def _redact(self, text: str, *, extra: tuple[str, ...] = ()) -> str:
        secrets = sorted(
            {s for s in (self._key_id, self._api_key, *extra) if s},
            key=len,
            reverse=True,
        )
        out = text
        for secret in secrets:
            out = out.replace(secret, _REDACTED)
        return out
