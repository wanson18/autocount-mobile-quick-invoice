"""Resolve the products named by a set of invoice lines.

Shared by both write paths. Creating an invoice and editing one ask exactly
the same question of the account book -- does this company's own book return
this item under exactly this code? -- and answering it differently in the two
places is how one path quietly gains a loophole the other does not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.config import CompanyConfig
from app.models.master_data import ProductSummary


class ProductNotInCompanyError(Exception):
    """A line names an item the selected company's account book does not have.

    Raised as a neutral error and translated by each caller into its own
    domain failure, so a bad item on the create path still reads as a draft
    validation error and one on the edit path as an edit error.
    """


class _Line(Protocol):
    item_id: str


class _ItemLookup(Protocol):
    async def get_item(
        self, company: CompanyConfig, item_id: str
    ) -> ProductSummary: ...


async def resolve_products(
    master_data: _ItemLookup,
    company: CompanyConfig,
    lines: Sequence[_Line],
) -> dict[str, ProductSummary]:
    """Every distinct line item, looked up in ``company``'s own account book.

    An item counts as resolved only when the book returns it under exactly
    the requested code, which is what stops an item belonging to the other
    company from reaching a payload. Each distinct code is fetched once,
    however many lines name it.
    """
    products: dict[str, ProductSummary] = {}
    for line in lines:
        if line.item_id in products:
            continue
        product = await master_data.get_item(company, line.item_id)
        if product.id != line.item_id or product.code != line.item_id:
            raise ProductNotInCompanyError(
                f"item {line.item_id!r} does not belong to selected company"
            )
        products[line.item_id] = product
    return products
