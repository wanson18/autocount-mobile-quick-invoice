"""Product (item) search endpoint.

``default_price`` is serialised as an exact string (never a binary float) so
the preview can present the accounting value precisely.
"""

from fastapi import APIRouter, Depends, Query

from app.config import get_company
from app.dependencies import get_master_data
from app.models.company import CompanyKey
from app.models.master_data import ProductSearchResponse

router = APIRouter(tags=["products"])


@router.get("/{company}/products", operation_id="searchProducts", response_model=ProductSearchResponse)
async def search_products(
    company: CompanyKey,
    q: str = Query(default="", max_length=100),
    master=Depends(get_master_data),
) -> dict:
    summaries = await master.search_items(get_company(company), q)
    return {
        "data": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "default_price": str(p.default_price),
            }
            for p in summaries
        ]
    }
