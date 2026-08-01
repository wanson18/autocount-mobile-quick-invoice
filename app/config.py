"""Server-side company configuration.

Maps each CompanyKey to the AutoCount account book used for every downstream
customer, item, address, invoice, PDF, and e-Invoice call. The account book
IDs are secrets: they come from the server environment, never from the client.
The client only ever supplies a CompanyKey.

Fails fast when an account book ID is missing or not unique, so a misconfigured
server cannot silently mix data between the two companies.
"""

import os
from dataclasses import dataclass
from typing import Mapping

from app.models.company import CompanyKey

ENV_ACCOUNT_BOOK_ENTERPRISE = "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE"
ENV_ACCOUNT_BOOK_SDN_BHD = "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD"

_DISPLAY_NAMES = {
    CompanyKey.ENTERPRISE: "Wanson Enterprise",
    CompanyKey.SDN_BHD: "Wanson Enterprise (M) Sdn Bhd",
}

_ENV_VARS = {
    CompanyKey.ENTERPRISE: ENV_ACCOUNT_BOOK_ENTERPRISE,
    CompanyKey.SDN_BHD: ENV_ACCOUNT_BOOK_SDN_BHD,
}


class CompanyConfigError(ValueError):
    """Raised when server-side company configuration is missing or invalid."""


@dataclass(frozen=True)
class CompanyConfig:
    key: CompanyKey
    name: str
    account_book_id: str


def _load(env: Mapping[str, str]) -> dict[CompanyKey, CompanyConfig]:
    configs = {}
    for key, env_var in _ENV_VARS.items():
        account_book_id = env.get(env_var, "").strip()
        if not account_book_id:
            raise CompanyConfigError(
                f"Missing environment variable {env_var!r} for company {key.value!r}; "
                "set it to the AutoCount account book ID server-side"
            )
        configs[key] = CompanyConfig(key=key, name=_DISPLAY_NAMES[key], account_book_id=account_book_id)
    if configs[CompanyKey.ENTERPRISE].account_book_id == configs[CompanyKey.SDN_BHD].account_book_id:
        raise CompanyConfigError("AutoCount account book IDs must be distinct across companies")
    return configs


def get_company(company: CompanyKey, env: Mapping[str, str] | None = None) -> CompanyConfig:
    """Resolve a company key to its server-side configuration.

    Never accepts an account book ID; anything that is not a known
    CompanyKey is rejected.
    """
    if env is None:
        env = os.environ
    if not isinstance(company, CompanyKey):
        raise CompanyConfigError(f"Unknown company key: {company!r}")
    return _load(env)[company]


def list_companies(env: Mapping[str, str] | None = None) -> list[CompanyConfig]:
    """All configured companies, in a stable order."""
    if env is None:
        env = os.environ
    configs = _load(env)
    return [configs[key] for key in CompanyKey]
