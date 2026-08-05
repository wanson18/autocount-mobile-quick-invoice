"""Normalised errors raised by the AutoCount integration boundary.

Every exception is safe to surface to callers: messages are sanitised at
construction time, so ``str`` and ``repr`` never leak the API key, Key ID, or
account-book ID, even when AutoCount echoes one back in its error text.

The hierarchy deliberately distinguishes four classes of failure so that
downstream services (idempotency, reconciliation) can react differently:

- ``AutoCountRejectedError`` �?" AutoCount answered with a non-2xx status.
- ``AutoCountTransportError`` �?" the request never produced an HTTP response
  (including a timeout on a read-like call).
- ``AutoCountAmbiguousWriteError`` �?" a timeout on an explicitly marked write,
  where AutoCount may or may not have applied the operation.
- ``AutoCountDataError`` �?" AutoCount answered 2xx but the success payload was
  malformed, inconsistent, or unsafe to use. Messages are static and never
  embed the raw body, so no account-book ID, credential, or taxpayer/customer
  data can leak.
"""

from __future__ import annotations


class AutoCountError(Exception):
    """Base class for all AutoCount client errors with safe message text."""

    def __init__(self, message: str) -> None:
        self._safe_message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self._safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._safe_message!r})"


class AutoCountConfigError(AutoCountError):
    """Client configuration is invalid (for example, empty credentials)."""


class AutoCountEndpointError(AutoCountError):
    """A caller-supplied endpoint is not a safe relative AutoCount path."""


class AutoCountRejectedError(AutoCountError):
    """AutoCount answered with a non-2xx status.

    Carries the HTTP status code as ``status_code`` and a sanitised AutoCount
    message when the error body provides one.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.status_code}, {self._safe_message!r})"


class AutoCountTransportError(AutoCountError):
    """The request failed before an HTTP response was received.

    Raised for read timeouts and any other ``httpx.RequestError``.
    """


class AutoCountAmbiguousWriteError(AutoCountError):
    """A write request timed out; AutoCount may or may not have applied it."""


class AutoCountDataError(AutoCountError):
    """A 2xx AutoCount success payload was malformed or unsafe to use.

    Raised by the master-data adapter when a success response cannot be
    normalised: missing or malformed envelopes, rows, totals, identity
    mismatches, or unsafe prices. The message is static text only and never
    includes the raw response body.
    """


class AutoCountUnsupportedError(AutoCountError):
    """The documented AutoCount API provides no supported mechanism.

    Raised by the adapter for operations the Cloud Accounting Integration API
    does not document (currently the official invoice PDF). The adapter
    boundary fails closed without an HTTP call; the operation stays disabled
    until AutoCount documents a supported mechanism.
    """
