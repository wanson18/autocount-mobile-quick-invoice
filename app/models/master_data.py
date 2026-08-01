"""Normalised read-only master-data representations from AutoCount.

These are the immutable summaries returned by ``AutoCountMasterDataAdapter``.
They intentionally carry no account-book identity: isolation is guaranteed by
the server-side ``CompanyConfig`` passed into every adapter call, never by
these values.

Identity rules:

- A customer's identity is its AutoCount code: ``id == code``.
- A product's identity is its AutoCount lookup code: ``id == code``.
- ``default_price`` is always an exact ``Decimal``, never a binary float.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CustomerSummary:
    """A customer from the selected account book."""

    id: str
    code: str
    name: str


@dataclass(frozen=True)
class DeliveryAddress:
    """A delivery address owned by one customer.

    ``id`` is a stable adapter ID derived from the owning customer's code, so
    an address object can never be accepted for another customer or account
    book.
    """

    id: str
    label: str
    address_text: str


@dataclass(frozen=True)
class ProductSummary:
    """An item from the selected account book, with its exact master price."""

    id: str
    code: str
    name: str
    default_price: Decimal
