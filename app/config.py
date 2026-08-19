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

# Verified Cloud report URL template, supplied by the user and stored server-side.
# It carries the account-book path (a secret) baked into the value and exposes a
# single {doc_key} placeholder filled with the server-confirmed AutoCount docKey.
# Never sent to the client and never embedded in browser assets or the OpenAPI schema.
ENV_CLOUD_INVOICE_URL_TEMPLATE = "AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE"

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


def get_cloud_invoice_url_template(env: Mapping[str, str] | None = None) -> str | None:
    """Return the verified Cloud report URL template, or ``None`` if unset.

    The template is server-side configuration: it embeds the account-book path
    (a secret) and exposes a single ``{doc_key}`` placeholder. The client never
    receives it; the cloud-report route fills ``{doc_key}`` with the
    server-confirmed AutoCount ``docKey`` before redirecting.
    """
    if env is None:
        env = os.environ
    template = env.get(ENV_CLOUD_INVOICE_URL_TEMPLATE, "").strip()
    return template or None
