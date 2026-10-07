"""Product (item) search endpoint.

``default_price`` is serialised as an exact string (never a binary float) so
the preview can present the accounting value precisely.
"""

from fastapi import APIRouter, Depends, Query

from app.config import get_company
from app.dependencies import get_master_data
from app.models.common import ItemResponse
from app.models.company import CompanyKey
from app.models.master_data import (
    ProductSearchItem,
    ProductSearchResponse,
    ProductUomItem,
)

router = APIRouter(tags=["products"])


def _item(p) -> ProductSearchItem:
    return ProductSearchItem(
        id=p.id,
        code=p.code,
        name=p.name,
        default_price=str(p.default_price),
        unit=p.unit,
        uoms=[
            ProductUomItem(name=u.name, rate=str(u.rate), price=str(u.price))
            for u in p.uoms
        ],
    )


@router.get("/{company}/products", operation_id="searchProducts", response_model=ProductSearchResponse)
async def search_products(
    company: CompanyKey,
    q: str = Query(default="", max_length=100),
    master=Depends(get_master_data),
) -> ProductSearchResponse:
    summaries = await master.search_items(get_company(company), q)
    return ProductSearchResponse(data=[_item(p) for p in summaries])


@router.get(
    "/{company}/products/{code:path}",
    response_model=ItemResponse[ProductSearchItem],
    include_in_schema=False,
)
async def get_product(
    company: CompanyKey,
    code: str,
    master=Depends(get_master_data),
) -> ItemResponse[ProductSearchItem]:
    """One item with the units of measure it can be sold in.

    The search list carries only the base unit: AutoCount's listing does not
    return a product's own units reliably, the single-product read does.
    """
    return ItemResponse(data=_item(await master.get_item(get_company(company), code)))
