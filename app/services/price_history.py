"""Latest-issued-price lookup for the confirmation preview.

For each exact customer/item pair in a confirmed draft, proposes the latest
unit price from a prior non-cancelled AutoCount invoice and identifies the
source invoice and date, so the GPT preview can be traced and the user can
accept or override the price before issue. Items with no prior invoice map
to ``None`` and the preview falls back to the master default price.
"""

from __future__ import annotations

from typing import Protocol

from app.config import CompanyConfig
from app.models.invoice import InvoiceDraftInput
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
    draft: InvoiceDraftInput,
) -> dict[str, PriceHistory | None]:
    """Latest issued price for every unique item in the draft.

    Deduplicates item lookups: duplicate lines sharing an ``item_id`` are
    resolved with one adapter call each.
    """
    unique_items = {line.item_id for line in draft.lines}
    return {
        item_id: await master_data.get_latest_price(
            company, draft.customer_id, item_id
        )
        for item_id in unique_items
    }
