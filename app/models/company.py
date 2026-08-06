"""Company keys recognised by the server.

Only these keys can be sent by the client. The AutoCount account book ID
behind each key is resolved server-side and is never a client input.
"""

from enum import Enum

from pydantic import BaseModel


class CompanyKey(str, Enum):
    ENTERPRISE = "enterprise"
    SDN_BHD = "sdn_bhd"


class CompanyListItem(BaseModel):
    key: str
    name: str


class CompanyListResponse(BaseModel):
    data: list[CompanyListItem]
