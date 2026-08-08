"""Customer and delivery-address endpoints.

The ``company`` path parameter is a ``CompanyKey``; FastAPI rejects anything
else with a 422 before the handler runs, and the server-side account book is
resolved from configuration only.
"""

from fastapi import APIRouter, Depends, Query

from app.config import get_company
from app.dependencies import get_master_data
from app.models.company import CompanyKey
from app.models.master_data import (
    CustomerSearchItem,
    CustomerSearchResponse,
    DeliveryAddressItem,
    DeliveryAddressListResponse,
)

router = APIRouter(tags=["customers"])


@router.get("/{company}/customers", operation_id="searchCustomers", response_model=CustomerSearchResponse)
async def search_customers(
    company: CompanyKey,
    q: str = Query(default="", max_length=100),
    master=Depends(get_master_data),
) -> CustomerSearchResponse:
    summaries = await master.search_customers(get_company(company), q)
    return CustomerSearchResponse(
        data=[
            CustomerSearchItem.model_validate(c, from_attributes=True)
            for c in summaries
        ]
    )


@router.get("/{company}/customers/{customer_id}/addresses", operation_id="listCustomerAddresses", response_model=DeliveryAddressListResponse)
async def get_customer_addresses(
    company: CompanyKey,
    customer_id: str,
    master=Depends(get_master_data),
) -> DeliveryAddressListResponse:
    addresses = await master.get_delivery_addresses(
        get_company(company), customer_id
    )
    return DeliveryAddressListResponse(
        data=[
            DeliveryAddressItem.model_validate(a, from_attributes=True)
            for a in addresses
        ]
    )
