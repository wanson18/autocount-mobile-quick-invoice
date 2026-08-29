"""Recent Invoice / result-screen office Print control contract.

Static-contract test against ``app/static/app.js``: Print sits next to Open
Cloud Report, posts only company + doc_no, and never mentions a Cloud URL,
account-book id, or printer name in the browser source.
"""

from tests.unit.test_recent_invoice_detail_cloud import (
    _read_app_js,
    _render_invoice_detail_body,
    _render_result_body,
)


def test_detail_has_print_next_to_cloud_report():
    body = _render_invoice_detail_body(_read_app_js())
    assert 'id="detail-print-office-btn">Print</button>' in body
    assert 'id="detail-open-cloud-report-btn">Open Cloud Report</button>' in body
    assert body.index("detail-print-office-btn") < body.index(
        "detail-open-cloud-report-btn"
    )


def test_result_has_print_next_to_cloud_report():
    body = _render_result_body(_read_app_js())
    assert 'id="print-office-btn">Print</button>' in body
    assert 'id="open-cloud-report-btn">Open Cloud Report</button>' in body
    assert body.index("print-office-btn") < body.index("open-cloud-report-btn")


def test_print_posts_same_origin_job_route():
    source = _read_app_js()
    assert "/print" in source
    assert "apiPostEmpty" in source
    assert "wireOfficePrint" in source
    # Status labels the phone shows.
    assert "Queued" in source
    assert "Printing" in source
    assert "Printed" in source
    assert "Print failed" in source


def test_browser_source_never_embeds_cloud_url_or_printer():
    source = _read_app_js().lower()
    assert "accounting-report.autocountcloud.com" not in source
    assert "{doc_key}" not in source
    assert "account_book" not in source
    assert "epsone85ff0" not in source
    assert "print_agent_token" not in source
    assert "cloud_report_url" not in source
