"""Invoice / result screens have no office Print control.

Print via Open Cloud Report (and the Epson app). The phone must not queue
jobs for a Windows print agent. Open Cloud Report, Edit lines, and Back stay.
"""

from tests.unit.app_js_source import (
    read_app_js,
    render_invoice_detail_body,
    render_result_body,
)


def _read_index_html():
    with open("app/static/index.html", encoding="utf-8") as fh:
        return fh.read()


def test_detail_keeps_cloud_report_and_edit_without_office_print():
    body = render_invoice_detail_body(read_app_js())
    html = _read_index_html()
    assert 'id="detail-open-cloud-report-btn">Open Cloud Report</button>' in body
    assert "Edit lines" in body
    assert 'id="back-btn">Back</button>' in html
    assert "Print</button>" not in body
    assert "print-office-btn" not in body
    assert "detail-print-office-btn" not in body
    assert "Office Print" not in body
    assert "office Epson" not in body
    assert "office printer" not in body.lower()


def test_result_keeps_cloud_report_without_office_print():
    body = render_result_body(read_app_js())
    assert 'id="open-cloud-report-btn">Open Cloud Report</button>' in body
    assert "Print</button>" not in body
    assert "print-office-btn" not in body
    assert "Office Print" not in body
    assert "office Epson" not in body


def test_phone_does_not_queue_office_print_jobs():
    source = read_app_js()
    assert "wireOfficePrint" not in source
    assert "/print" not in source
    assert "apiPostEmpty" not in source
    assert "printPollTimer" not in source
    assert "PRINT_AGENT" not in source
    assert "Queued — sending to the office printer" not in source
    assert "Printing on the office Epson" not in source


def test_browser_source_never_embeds_cloud_url_or_printer():
    source = read_app_js().lower()
    html = _read_index_html().lower()
    for blob in (source, html):
        assert "accounting-report.autocountcloud.com" not in blob
        assert "{doc_key}" not in blob
        assert "account_book" not in blob
        assert "epsone85ff0" not in blob
        assert "print_agent_token" not in blob
        assert "cloud_report_url" not in blob
        assert "print-agent" not in blob
