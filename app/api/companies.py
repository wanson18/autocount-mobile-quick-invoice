"""Company listing endpoint.

Returns the client-safe company keys and display names; the server-side
AutoCount account-book IDs are never part of the response.
"""

from fastapi import APIRouter

from app.config import list_companies

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", operation_id="listCompanies")
def get_companies() -> dict:
    return {
        "data": [
            {"key": listing.key.value, "name": listing.name}
            for listing in list_companies()
        ]
    }
