"""Task 1 — Configuration and Company Isolation.

Acceptance checks:
- Account-book IDs are distinct.
- Browser cannot supply arbitrary account-book IDs.
- All downstream calls use the selected server-side company configuration.
- Public company listings never expose the server-side account book ID.
"""

from dataclasses import asdict

import pytest

from app.config import (
    CompanyConfig,
    CompanyConfigError,
    CompanyListing,
    get_company,
    list_companies,
)
from app.models.company import CompanyKey

ENV = {
    "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE": "ab-wanson-enterprise-001",
    "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD": "ab-wanson-sdn-bhd-001",
}


def test_account_book_ids_are_distinct():
    enterprise = get_company(CompanyKey.ENTERPRISE, ENV)
    sdn_bhd = get_company(CompanyKey.SDN_BHD, ENV)
    assert enterprise.account_book_id != sdn_bhd.account_book_id


def test_enterprise_maps_to_its_server_side_account_book():
    config = get_company(CompanyKey.ENTERPRISE, ENV)
    assert config.account_book_id == "ab-wanson-enterprise-001"


def test_sdn_bhd_maps_to_its_server_side_account_book():
    config = get_company(CompanyKey.SDN_BHD, ENV)
    assert config.account_book_id == "ab-wanson-sdn-bhd-001"


def test_display_names_match_spec():
    assert get_company(CompanyKey.ENTERPRISE, ENV).name == "Wanson Enterprise"
    assert get_company(CompanyKey.SDN_BHD, ENV).name == "Wanson Enterprise (M) Sdn Bhd"


def test_missing_account_book_env_var_raises():
    env = {k: v for k, v in ENV.items() if k != "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD"}
    with pytest.raises(CompanyConfigError, match="AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD"):
        get_company(CompanyKey.SDN_BHD, env)


def test_duplicate_account_book_ids_rejected():
    env = {
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE": "same-book",
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD": "same-book",
    }
    with pytest.raises(CompanyConfigError, match="distinct"):
        get_company(CompanyKey.ENTERPRISE, env)


def test_unknown_company_key_rejected():
    with pytest.raises(CompanyConfigError):
        get_company("attacker-supplied-company", ENV)


def test_company_key_enum_rejects_arbitrary_values():
    with pytest.raises(ValueError):
        CompanyKey("attacker-supplied-account-book-id")


def test_list_companies_returns_both_in_stable_order():
    listings = list_companies(ENV)
    assert [c.key for c in listings] == [CompanyKey.ENTERPRISE, CompanyKey.SDN_BHD]


def test_list_companies_returns_client_safe_representation():
    listings = list_companies(ENV)
    assert all(isinstance(c, CompanyListing) for c in listings)
    assert [c.name for c in listings] == ["Wanson Enterprise", "Wanson Enterprise (M) Sdn Bhd"]


def test_listing_public_shape_has_only_key_and_name():
    listing = list_companies(ENV)[0]
    assert asdict(listing) == {
        "key": CompanyKey.ENTERPRISE,
        "name": "Wanson Enterprise",
    }


def test_list_companies_never_exposes_account_book_id():
    for listing in list_companies(ENV):
        assert not hasattr(listing, "account_book_id")


def test_missing_config_also_fails_public_listing():
    env = {k: v for k, v in ENV.items() if k != "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE"}
    with pytest.raises(CompanyConfigError, match="AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE"):
        list_companies(env)


def test_duplicate_config_also_fails_public_listing():
    env = {
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE": "same-book",
        "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD": "same-book",
    }
    with pytest.raises(CompanyConfigError, match="distinct"):
        list_companies(env)


def test_internal_resolver_returns_server_side_config():
    config = get_company(CompanyKey.ENTERPRISE, ENV)
    assert isinstance(config, CompanyConfig)
    assert config.key is CompanyKey.ENTERPRISE
    assert config.account_book_id == "ab-wanson-enterprise-001"


def test_internal_resolver_rejects_account_book_id_input():
    with pytest.raises(CompanyConfigError):
        get_company("ab-wanson-enterprise-001", ENV)


def test_internal_resolver_rejects_none():
    with pytest.raises(CompanyConfigError):
        get_company(None, ENV)
