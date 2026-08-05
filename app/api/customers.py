"""Customer and delivery-address endpoints.

The ``company`` path parameter is a ``CompanyKey``; FastAPI rejects anything
else with a 422 before the handler runs, and the server-side account book is
resolved from configuration only.
"""

from fastapi import APIRouter, Depends, Query

from app.config import get_company
from app.dependencies import get_master_data
from app.models.company import CompanyKey

router = APIRouter(tags=["customers"])


@router.get("/{company}/customers", operation_id="searchCustomers")
async def search_customers(
    company: CompanyKey,
    q: str = Query(default="", max_length=100),
    master=Depends(get_master_data),
) -> dict:
    summaries = await master.search_customers(get_company(company), q)
    return {
        "data": [
            {"id": c.id, "code": c.code, "name": c.name} for c in summaries
        ]
    }


@router.get("/{company}/customers/{customer_id}/addresses", operation_id="listCustomerAddresses")
async def get_customer_addresses(
    company: CompanyKey,
    customer_id: str,
    master=Depends(get_master_data),
) -> dict:
    addresses = await master.get_delivery_addresses(
        get_company(company), customer_id
    )
    return {
        "data": [
            {"id": a.id, "label": a.label, "address_text": a.address_text}
            for a in addresses
        ]
    }
