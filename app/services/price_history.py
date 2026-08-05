"""Latest-issued-price lookup for the confirmation preview.

For each exact customer/item pair, proposes the latest unit price from a
prior non-cancelled AutoCount invoice and identifies the source invoice and
date, so the GPT preview can be traced and the user can accept or override
the price before issue. Items with no prior invoice map to ``None`` and the
preview falls back to the master default price.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.config import CompanyConfig
from app.models.master_data import PriceHistory


class PriceHistoryPort(Protocol):
    async def get_latest_price(
        self,
        company: CompanyConfig,
        customer_id: str,
        item_id: str,
    ) -> PriceHistory | None: ...


async def get_price_history(
    master_data: PriceHistoryPort,
    company: CompanyConfig,
    customer_id: str,
    item_ids: Iterable[str],
) -> dict[str, PriceHistory | None]:
    """Latest issued price for every unique item for one customer.

    Deduplicates item lookups: repeated ``item_ids`` are resolved with one
    adapter call each.
    """
    unique_items = dict.fromkeys(item_ids)
    return {
        item_id: await master_data.get_latest_price(
            company, customer_id, item_id
        )
        for item_id in unique_items
    }
