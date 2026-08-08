"""Shared generic response envelopes for the HTTP API.

Every JSON-returning endpoint wraps its payload in ``{"data": ...}``; these
generics give that envelope one definition instead of a hand-copied
``BaseModel`` per endpoint.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ItemResponse(BaseModel, Generic[T]):
    data: T


class ListResponse(BaseModel, Generic[T]):
    data: list[T]
