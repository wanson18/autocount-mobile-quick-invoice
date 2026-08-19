"""Recent Invoice detail Cloud handoff control contract.

Static-contract test for the browser view in ``app/static/app.js``: this repo
has no browser-JS test harness, so we verify the required control contract
directly against the production source. The contract mirrors the post-issue
result screen's Cloud handoff but lives on the Recent Invoice detail screen:

* every detail screen shows "Open Cloud Report" opening the same-origin hidden
  route ``/api/{company}/invoices/{doc_no}/cloud-report`` in a new tab with
  ``noopener,noreferrer`` (the server, not the browser, resolves the Cloud URL
  from the confirmed AutoCount docKey);
* "Copy invoice number" copies the server-returned ``inv.doc_no`` via the
  existing ``showBanner(message, kind)`` signature;
* a short note says Print, Export PDF, and Share happen in AutoCount Cloud;
* the existing "Edit lines" action and the cancelled/old read-only messages are
  preserved;
* detail-screen buttons are scoped to ``invoice-detail-actions`` and use detail
  IDs that do not collide with the post-issue result screen's controls.
"""

import re

import pytest

APP_JS = "app/static/app.js"


def _read_app_js():
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def _render_invoice_detail_body(source):
    match = re.search(
        r"function renderInvoiceDetail\(\)\s*\{(.*?)\n  \}",
        source,
        re.DOTALL,
    )
    assert match, "renderInvoiceDetail() not found in app.js"
    return match.group(1)


@pytest.fixture
def detail_body():
    return _render_invoice_detail_body(_read_app_js())


def test_detail_opens_same_origin_cloud_report_route_in_new_tab(detail_body):
    # Same-origin hidden route, not a browser-built Cloud URL.
    assert "/api/" in detail_body
    assert "/cloud-report" in detail_body
    assert "state.company.key" in detail_body
    assert "inv.doc_no" in detail_body
    # New tab, no opener/referrer leakage.
    assert 'window.open(' in detail_body
    assert "noopener,noreferrer" in detail_body


def test_detail_copies_server_invoice_number_via_show_banner(detail_body):
    assert "Copy invoice number" in detail_body
    assert "inv.doc_no" in detail_body
    # Uses the existing two-argument banner signature, not a reinvented one.
    assert "showBanner(" in detail_body


def test_detail_notes_print_pdf_share_happen_in_cloud(detail_body):
    note = detail_body.lower()
    assert "print" in note
    assert "export pdf" in note
    assert "share" in note
    assert "autocount" in note


def test_detail_preserves_edit_lines_and_readonly_messages(detail_body):
    assert "Edit lines" in detail_body
    assert "is_cancelled" in detail_body
    assert "EDIT_WINDOW_DAYS" in detail_body


def test_detail_buttons_scoped_and_non_colliding_ids(detail_body):
    # Rendered into the scoped detail actions container, not a bare id reused
    # by the post-issue result screen (which uses open-cloud-report-btn).
    assert "invoice-detail-actions" in detail_body
    assert "detail-open-cloud-report-btn" in detail_body
    assert "detail-copy-invoice-number-btn" in detail_body
    # No collision with the post-issue result screen's own (un-prefixed) ids.
    assert 'id="open-cloud-report-btn"' not in detail_body
    assert 'id="copy-invoice-number-btn"' not in detail_body
    assert 'id="edit-invoice-btn"' in detail_body


def test_detail_cloud_actions_use_the_stacked_action_wrapper(detail_body):
    assert 'class="result-actions detail-cloud-actions"' in detail_body
