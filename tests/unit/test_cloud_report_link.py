"""Cloud report deep-link URL contract.

Verifies the URL builder that turns the server-confirmed AutoCount ``docKey``
into the verified Cloud report URL. The builder must never embed account-book
identity, API keys, or any AutoCount response data in the produced URL, and it
must refuse malformed templates rather than leak a partial or insecure link.
"""

import pytest

from app.services.cloud_report_link import build_cloud_report_url


def test_cloud_report_url_substitutes_doc_key_without_exposing_account_book():
    url = build_cloud_report_url(
        "https://cloud.test.invalid/invoice?docKey={doc_key}",
        doc_key="9001",
    )
    assert url == "https://cloud.test.invalid/invoice?docKey=9001"
    assert "account" not in url.lower()
    assert "api" not in url.lower()


def test_cloud_report_url_rejects_blank_doc_key():
    with pytest.raises(ValueError, match="doc_key"):
        build_cloud_report_url(
            "https://cloud.test.invalid/invoice?docKey={doc_key}",
            doc_key=" ",
        )


def test_cloud_report_url_requires_https():
    with pytest.raises(ValueError, match="https"):
        build_cloud_report_url(
            "http://cloud.test.invalid/invoice?docKey={doc_key}",
            doc_key="9001",
        )


def test_cloud_report_url_rejects_account_book_placeholder():
    with pytest.raises(ValueError, match="account-book|account_book"):
        build_cloud_report_url(
            "https://cloud.test.invalid/{account_book_id}/invoice?docKey={doc_key}",
            doc_key="9001",
        )


def test_cloud_report_url_requires_exactly_one_doc_key_placeholder():
    with pytest.raises(ValueError, match="exactly one"):
        build_cloud_report_url(
            "https://cloud.test.invalid/invoice?docKey=9001",
            doc_key="9002",
        )

    with pytest.raises(ValueError, match="exactly one"):
        build_cloud_report_url(
            "https://cloud.test.invalid/invoice?docKey={doc_key}&copy={doc_key}",
            doc_key="9002",
        )
