"""Shared access to the browser source for static-contract tests.

This repo has no browser-JS test harness, so contract tests verify the
required controls directly against the production ``app/static/app.js``
source. These helpers live here — not inside one test module — so several
contract tests can scrape the same render functions without importing private
helpers from each other.
"""

import re

APP_JS = "app/static/app.js"


def read_app_js() -> str:
    with open(APP_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def render_invoice_detail_body(source: str) -> str:
    match = re.search(
        r"function renderInvoiceDetail\(\)\s*\{(.*?)\n  \}",
        source,
        re.DOTALL,
    )
    assert match, "renderInvoiceDetail() not found in app.js"
    return match.group(1)


def render_result_body(source: str) -> str:
    match = re.search(
        r"function renderResultScreen\(\)\s*\{(.*?)\n  \}",
        source,
        re.DOTALL,
    )
    assert match, "renderResultScreen() not found in app.js"
    return match.group(1)
