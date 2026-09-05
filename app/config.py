"""Server-side company configuration.

Maps each CompanyKey to the AutoCount account book used for every downstream
customer, item, address, invoice, PDF, and e-Invoice call. The account book
IDs are secrets: they come from the server environment, never from the client.
The client only ever supplies a CompanyKey.

The internal resolver (``get_company``) returns the full server-side
``CompanyConfig``, including the account book ID, for downstream AutoCount
services. The public listing (``list_companies``) returns client-safe
``CompanyListing`` objects that never expose the account book ID.

Fails fast when an account book ID is missing or not unique, so a misconfigured
server cannot silently mix data between the two companies.
"""

import os
from dataclasses import dataclass
from typing import Mapping

from app.models.company import CompanyKey

ENV_ACCOUNT_BOOK_ENTERPRISE = "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE"
ENV_ACCOUNT_BOOK_SDN_BHD = "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD"

# AutoCount issues a distinct Key-ID/API-Key pair per account book (each
# company has its own credentials, not a shared one). These are optional:
# when unset for a company, that company falls back to the client-wide
# AUTOCOUNT_API_KEY_ID/AUTOCOUNT_API_KEY (see app/dependencies.py), which
# keeps single-company and legacy shared-credential deployments working.
ENV_KEY_ID_ENTERPRISE = "AUTOCOUNT_KEY_ID_WANSON_ENTERPRISE"
ENV_API_KEY_ENTERPRISE = "AUTOCOUNT_API_KEY_WANSON_ENTERPRISE"
ENV_KEY_ID_SDN_BHD = "AUTOCOUNT_KEY_ID_WANSON_SDN_BHD"
ENV_API_KEY_SDN_BHD = "AUTOCOUNT_API_KEY_WANSON_SDN_BHD"

# Client-wide AutoCount credentials (a company's per-company pair above falls
# back to these when unset) and the idempotency-repository storage locations.
# Defined here so app/config.py is the single home for environment variable
# names; app/dependencies.py and the diagnostic scripts import them.
ENV_KEY_ID = "AUTOCOUNT_API_KEY_ID"
ENV_API_KEY = "AUTOCOUNT_API_KEY"
ENV_DB_PATH = "INVOICE_REQUESTS_DB"
DEFAULT_DB_PATH = "data/invoice_requests.db"
ENV_POSTGRES_URL = "POSTGRES_URL"
ENV_DATABASE_URL = "DATABASE_URL"

# Verified AutoCount Cloud report identities for the invoice report, scoped to
# the selected company: the server resolves the CompanyKey to its own account
# book (above) and its own report name (below), substituting the
# server-confirmed docKey before redirecting. These are server-side only — the
# client never supplies or sees the base URL, account book, or report name.
# The Sdn Bhd report name keeps its trailing space because that is the
# configured AutoCount report identity.
CLOUD_REPORT_BASE_URL = "https://accounting-report.autocountcloud.com"
CLOUD_INVOICE_REPORT_NAMES = {
    CompanyKey.ENTERPRISE: "Wanson Enterprise Invoice",
    CompanyKey.SDN_BHD: "WANSON SDN BHD E-INVOICE ",
}

_DISPLAY_NAMES = {
    CompanyKey.ENTERPRISE: "Wanson Enterprise",
    CompanyKey.SDN_BHD: "Wanson Enterprise (M) Sdn Bhd",
}

_ENV_VARS = {
    CompanyKey.ENTERPRISE: ENV_ACCOUNT_BOOK_ENTERPRISE,
    CompanyKey.SDN_BHD: ENV_ACCOUNT_BOOK_SDN_BHD,
}

_CREDENTIAL_ENV_VARS = {
    CompanyKey.ENTERPRISE: (ENV_KEY_ID_ENTERPRISE, ENV_API_KEY_ENTERPRISE),
    CompanyKey.SDN_BHD: (ENV_KEY_ID_SDN_BHD, ENV_API_KEY_SDN_BHD),
}


class CompanyConfigError(ValueError):
    """Raised when server-side company configuration is missing or invalid."""


@dataclass(frozen=True)
class CompanyConfig:
    """Server-side company configuration, including the secret account book ID.

    Internal only: returned by ``get_company`` for downstream AutoCount
    services, never by the public listing.
    """

    key: CompanyKey
    name: str
    account_book_id: str
    key_id: str | None = None
    api_key: str | None = None


@dataclass(frozen=True)
class CompanyListing:
    """Client-safe company representation for the public listing.

    Contains only the company key and display name; the server-side account
    book ID is intentionally absent.
    """

    key: CompanyKey
    name: str


def _load(env: Mapping[str, str]) -> dict[CompanyKey, CompanyConfig]:
    configs = {}
    for key, env_var in _ENV_VARS.items():
        account_book_id = env.get(env_var, "").strip()
        if not account_book_id:
            raise CompanyConfigError(
                f"Missing environment variable {env_var!r} for company {key.value!r}; "
                "set it to the AutoCount account book ID server-side"
            )
        key_id, api_key = _load_credentials(env, key)
        configs[key] = CompanyConfig(
            key=key,
            name=_DISPLAY_NAMES[key],
            account_book_id=account_book_id,
            key_id=key_id,
            api_key=api_key,
        )
    if configs[CompanyKey.ENTERPRISE].account_book_id == configs[CompanyKey.SDN_BHD].account_book_id:
        raise CompanyConfigError("AutoCount account book IDs must be distinct across companies")
    return configs


def _load_credentials(env: Mapping[str, str], key: CompanyKey) -> tuple[str | None, str | None]:
    """Read this company's optional per-company Key-ID/API-Key pair.

    Each AutoCount account book has its own credentials, so both env vars
    must be set together for a company to use per-company auth; leaving both
    unset is also valid and falls back to the client-wide credentials
    (AUTOCOUNT_API_KEY_ID/AUTOCOUNT_API_KEY). Setting only one is a
    misconfiguration and fails closed rather than silently using a partial or
    wrong credential.
    """
    key_id_var, api_key_var = _CREDENTIAL_ENV_VARS[key]
    key_id = env.get(key_id_var, "").strip()
    api_key = env.get(api_key_var, "").strip()
    if bool(key_id) != bool(api_key):
        raise CompanyConfigError(
            f"{key_id_var!r} and {api_key_var!r} must be set together for company "
            f"{key.value!r}, or both left unset to use the shared AutoCount credentials"
        )
    return (key_id, api_key) if key_id else (None, None)


def get_company(company: CompanyKey, env: Mapping[str, str] | None = None) -> CompanyConfig:
    """Resolve a company key to its server-side configuration.

    Internal resolver for downstream AutoCount services. Never accepts an
    account book ID; anything that is not a known CompanyKey is rejected.
    """
    if env is None:
        env = os.environ
    if not isinstance(company, CompanyKey):
        raise CompanyConfigError(f"Unknown company key: {company!r}")
    return _load(env)[company]


def list_companies(env: Mapping[str, str] | None = None) -> list[CompanyListing]:
    """All configured companies, in a stable order, as client-safe listings.

    The account book ID is never part of the returned representation.
    """
    if env is None:
        env = os.environ
    configs = _load(env)
    return [CompanyListing(key=config.key, name=config.name) for config in (configs[key] for key in CompanyKey)]
