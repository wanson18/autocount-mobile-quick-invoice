"""Company listing endpoint.

Returns the client-safe company keys and display names; the server-side
AutoCount account-book IDs are never part of the response.
"""

from fastapi import APIRouter

from app.config import list_companies
from app.models.company import CompanyListItem, CompanyListResponse

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", operation_id="listCompanies", response_model=CompanyListResponse)
def get_companies() -> CompanyListResponse:
    return CompanyListResponse(
        data=[
            CompanyListItem(key=listing.key, name=listing.name)
            for listing in list_companies()
        ]
    )
