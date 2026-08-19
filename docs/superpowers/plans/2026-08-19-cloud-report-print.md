# Cloud Report Print Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Give the user a safe post-issue path to print, export, or share the invoice using the exact AutoCount Cloud report format.

**Architecture:** Keep invoice creation and AutoCount API authentication unchanged. First verify the Cloud report route with an existing issued invoice; then implement either a safe Cloud deep-link handoff or a generic Cloud-app handoff with invoice-number copy. The app will only offer direct PDF printing/sharing if AutoCount confirms a supported official report/PDF endpoint.

This plan does not implement a custom PDF or an undocumented report API. If the live check discovers an official AutoCount report/PDF endpoint instead of a usable Cloud handoff, stop after Task 1 and revise this plan before writing code.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, httpx, existing AutoCount adapter, mobile JavaScript PWA, pytest, and manual authenticated browser verification.

**Spec:** docs/superpowers/specs/2026-08-19-cloud-report-print-design.md

## Global Constraints

- Keep saveApprove: true and submitEInvoice: false on the existing issue path.
- Use the AutoCount Cloud report as the output authority; do not generate a replacement invoice PDF.
- Do not scrape production HTML or automate Cloud UI clicks when no supported mechanism exists.
- Do not expose account-book IDs, API keys, credentials, or taxpayer data in
  browser assets or public app JSON. The Cloud redirect may resolve to
  AutoCount's own URL containing its required server-configured account-book
  path; that value must remain out of the app's JavaScript, OpenAPI schema, and
  client-generated URLs.
- Printing, exporting, or sharing is read-only after issue and must never create, edit, void, approve, post, or submit an invoice.
- Use the server-confirmed invoice_number/document number to look up the
  invoice, then use the server-confirmed invoice_id/document key (`docKey`) to
  build the Cloud report URL. The Cloud report URL supplied for this project
  uses `docKey`, not the invoice number.
- Run python -m pytest tests/ before each implementation commit and perform manual Cloud verification before claiming completion.

---

### Task 1: Run and record the Cloud-report discovery gate

**Files:**
- Create: docs/autocount/cloud-report-print-spike.md
- Read: docs/autocount/pdf-spike.md
- Read: app/static/app.js lines 899-1008
- Read: app/api/invoices.py lines 70-246

**Interfaces:**
- Consumes: An existing issued invoice number from the current invoice-list/detail flow; no new invoice is created for this spike.
- Produces: A dated decision record selecting either the deep-link handoff or the generic Cloud handoff. No implementation task may proceed until this record exists.

- [ ] **Step 1: Establish a clean baseline on the isolated branch**

Run:

~~~powershell
git status --short --branch
python -m pytest tests/ -q
~~~

Expected: the branch is codex/cloud-report-print-test, the working tree has no feature changes, and the existing suite passes.

- [ ] **Step 2: Select an existing invoice without creating a test document**

Use the deployed mobile page's existing Recent invoices view or AutoCount Cloud's invoice list to select one already issued Sdn Bhd invoice. Record only its document number and company key in the local spike note; do not create, edit, void, approve, post, or submit anything.

- [ ] **Step 3: Verify the exact Cloud report path manually**

In the already-authenticated AutoCount Cloud session:

1. Open the selected invoice.
2. Open the configured invoice report/preview.
3. Confirm the preview matches the user's current Cloud invoice format.
4. Use Cloud's own Print or Export PDF control once.
5. Record the visible menu path and the final browser URL without recording credentials or account-book secrets.
6. Open the recorded URL in a fresh tab in the same authenticated session and check whether it returns to the same invoice/report.

Expected: the report is produced by AutoCount Cloud, not by this app. If a stable direct link exists, it must identify the invoice with a safe document value only and must not require the app to scrape Cloud HTML.

- [ ] **Step 4: Write the discovery result**

Create docs/autocount/cloud-report-print-spike.md with these headings:

~~~markdown
# AutoCount Cloud Report Print Spike

Date: 2026-08-19
Company: Wanson Enterprise (M) Sdn Bhd
Invoice used: record the existing invoice number used in the check

## Cloud menu path
Record the exact visible path observed in AutoCount Cloud.

## Output check
- Configured Cloud report format: record PASS or FAIL and the observation
- Cloud Print or Export PDF: record PASS or FAIL and the observation
- Stable invoice deep link in the same authenticated session: record PASS or FAIL and the observation
- Link contains only a safe document identifier: record PASS or FAIL and the observation

## Decision
Record either deep-link handoff or generic Cloud handoff.

## Safety result
No invoice write, approval, posting, e-Invoice submission, or automated Cloud UI scraping was performed.
~~~

Write the actual observed values before committing; do not commit instruction text as if it were evidence.

- [ ] **Step 5: Commit the gate evidence**

Run:

~~~powershell
git add docs/autocount/cloud-report-print-spike.md
git commit -m "docs: record Cloud report print route"
~~~

Expected: one documentation-only commit containing the observed Cloud-report decision.

**Gate:** If the Cloud report format or authenticated handoff cannot be verified, stop and report the blocker. Do not implement a custom PDF. If a stable deep link passes, continue to Task 2. Otherwise continue to Task 3.

---

### Task 2: Implement the Cloud invoice deep-link handoff

**Condition:** Execute only when Task 1 records a safe Cloud report route. The
route's live stability and visual report/Print behavior remain explicit Task 4
conditions because the current browser runtime cannot perform the authenticated
fresh-tab check.

**Files:**
- Modify: app/config.py
- Modify: app/api/invoices.py
- Create: app/services/cloud_report_link.py
- Modify: app/static/app.js lines 899-915
- Modify: app/static/index.html lines 335-353
- Create: tests/unit/test_cloud_report_link.py
- Modify: tests/unit/test_api.py
- Modify: README.md lines 32-36 and 148-154

**Interfaces:**
- Consumes: CompanyKey, a server-side Cloud URL template verified in Task 1,
  and the issued document number used for the server-side read-back.
- Produces: GET /api/{company}/invoices/{doc_no}/cloud-report, hidden from the Custom GPT schema, returning an HTTP redirect to the verified Cloud report URL.
- Exact helper: build_cloud_report_url(template: str, doc_key: str) -> str.

- [ ] **Step 1: Write URL-builder tests before implementation**

Add tests covering the URL contract:

~~~python
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
~~~

The production configuration must use the exact safe template recorded in Task 1. The unit-test URL is a non-routable test fixture and is not a production value.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~powershell
python -m pytest tests/unit/test_cloud_report_link.py -q
~~~

Expected: FAIL because the URL builder does not yet exist.

- [ ] **Step 3: Implement the server-side URL builder and redirect**

Add ENV_CLOUD_INVOICE_URL_TEMPLATE and a server-side getter in app/config.py,
then add build_cloud_report_url(template: str, doc_key: str) -> str in
app/services/cloud_report_link.py. The production template must match the
Cloud route supplied by the user, with the account-book path kept in the
server environment, for example:

~~~text
https://accounting-report.autocountcloud.com/rpt/<server-account-book-key>/invoice?reportName=WANSON+SDN+BHD+E-INVOICE+&docKey={doc_key}
~~~

It must:

1. reject a blank document key;
2. substitute only the URL-encoded {doc_key} value;
3. reject templates containing {account_book_id}, API-key text, or credential placeholders;
4. require an HTTPS URL; and
5. return no AutoCount response data or credentials.

Add the hidden route GET /api/{company}/invoices/{doc_no}/cloud-report in
app/api/invoices.py. Resolve the company server-side, call
read_invoice(master, company, doc_no) to prove the document exists in the
selected account book, then pass `invoice.id` (the server-confirmed AutoCount
`docKey`) to the URL builder and return RedirectResponse to the verified
template. A missing invoice must use the existing invoice_not_found response
rather than redirecting.

- [ ] **Step 4: Run focused API tests**

Add these cases to tests/unit/test_api.py using the existing fake master:

~~~python
def test_cloud_report_redirect_uses_verified_invoice_and_hides_account_book(api, monkeypatch):
    monkeypatch.setenv(
        "AUTOCOUNT_CLOUD_INVOICE_URL_TEMPLATE",
        "https://cloud.test.invalid/invoice?docKey={doc_key}",
    )
    client, _, _ = api
    response = client.get(
        "/api/sdn_bhd/invoices/INV-2026-0001/cloud-report",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://cloud.test.invalid/invoice?docKey=inv-1"
    )
    assert SDN_BHD_AB not in response.text


def test_cloud_report_redirect_does_not_create_or_update_invoice(api):
    client, _, _ = api
    response = client.get(
        "/api/sdn_bhd/invoices/INV-2026-0001/cloud-report",
        follow_redirects=False,
    )
    assert response.status_code in {307, 501}
~~~

The test fixture must provide the existing get_invoice read-back expected by the route; it must not call the invoice service's create or update methods.

- [ ] **Step 5: Add result-screen handoff controls**

In app/static/app.js, keep the existing issued invoice number visible and add:

- Open Cloud Report, which opens the hidden route with state.company.key and r.invoice_number in a new tab using noopener,noreferrer;
- Copy invoice number, which copies the same server-confirmed number and shows a success banner or a clear manual-copy fallback.

Do not call these controls Share as PDF because the app is opening Cloud rather than receiving a PDF. The instruction beside the buttons must say that Print, Export PDF, and Share are performed in the AutoCount Cloud report screen.

- [ ] **Step 6: Verify the frontend does not change issue behavior**

Run:

~~~powershell
node --check app/static/app.js
python -m pytest tests/ -q
~~~

Expected: JavaScript syntax passes, the full Python suite passes, and no issue payload or saveApprove/submitEInvoice behavior changes.

- [ ] **Step 7: Commit the handoff implementation**

Run:

~~~powershell
git add app/config.py app/api/invoices.py app/services/cloud_report_link.py app/static/app.js app/static/index.html tests/unit/test_cloud_report_link.py tests/unit/test_api.py README.md
git commit -m "feat: open issued invoices in Cloud report"
~~~

---

### Task 3: Implement the generic Cloud handoff when no safe deep link exists

**Condition:** Execute only when Task 1 records that no stable, safe invoice deep link exists.

**Files:**
- Modify: app/config.py
- Modify: app/api/companies.py
- Modify: app/static/app.js lines 899-915
- Modify: app/static/index.html lines 335-353
- Create: tests/unit/test_cloud_app_link.py
- Modify: tests/unit/test_api.py
- Modify: README.md lines 32-36 and 148-154

**Interfaces:**
- Consumes: A server-configured HTTPS AutoCount Cloud app origin that contains no account-book ID, API key, or credential.
- Produces: A client-safe GET /api/cloud-app response containing only the configured Cloud app URL, plus result-screen controls to open it and copy the issued document number.

- [ ] **Step 1: Write the safe Cloud-app configuration tests**

Add tests for the configuration boundary:

~~~python
def test_cloud_app_url_requires_https_and_returns_only_the_public_origin():
    assert get_cloud_app_url(
        {"AUTOCOUNT_CLOUD_APP_URL": "https://cloud.test.invalid"}
    ) == "https://cloud.test.invalid"


def test_cloud_app_url_rejects_non_https_values():
    with pytest.raises(CompanyConfigError, match="Cloud app URL"):
        get_cloud_app_url(
            {"AUTOCOUNT_CLOUD_APP_URL": "http://cloud.test.invalid"}
        )
~~~

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~powershell
python -m pytest tests/unit/test_cloud_app_link.py -q
~~~

Expected: FAIL because the public Cloud-app URL configuration does not yet exist.

- [ ] **Step 3: Add the client-safe configuration and endpoint**

Add AUTOCOUNT_CLOUD_APP_URL to app/config.py as an HTTPS-only public app origin. Reject URLs containing @, query strings, fragments, or known credential/account-book environment values. Add GET /api/cloud-app to app/api/companies.py; return only the validated public URL and never return a CompanyConfig or account-book ID.

- [ ] **Step 4: Add generic handoff controls to the result screen**

Load the public Cloud app URL once after the app starts. On a successful issue, show:

- Open AutoCount Cloud, opening the configured origin in a new tab;
- Copy invoice number, copying the server-confirmed document number.

The result message must say: Search this invoice number in AutoCount Cloud, then use your Cloud report's Print or Export PDF control. Do not show a fake PDF download or a custom print view.

- [ ] **Step 5: Test the fallback path and full suite**

Run:

~~~powershell
node --check app/static/app.js
python -m pytest tests/ -q
~~~

Expected: all tests pass; the only new browser capability is opening Cloud and copying a document number.

- [ ] **Step 6: Commit the generic handoff**

~~~powershell
git add app/config.py app/api/companies.py app/static/app.js app/static/index.html tests/unit/test_cloud_app_link.py tests/unit/test_api.py README.md
git commit -m "feat: hand off issued invoices to Cloud"
~~~

---

### Task 4: Perform the end-to-end Cloud report verification

**Files:**
- Modify: docs/testing/manual-test-checklist.md
- Modify: README.md

**Interfaces:**
- Consumes: The selected implementation from Task 2 or Task 3 and one real issued invoice.
- Produces: Evidence that the exact Cloud report can be printed/exported/shared without a second accounting write.

- [ ] **Step 1: Deploy the branch preview**

Deploy only the branch preview using the repository's existing Vercel workflow. Configure the existing AutoCount credentials server-side and configure only the safe Cloud URL setting selected by Task 1. Do not put credentials or account-book IDs in Vercel client variables.

- [ ] **Step 2: Verify the post-issue workflow**

On the branch preview:

1. Issue one normal invoice through the existing flow.
2. Confirm the result shows exactly one AutoCount invoice number.
3. Tap the Cloud handoff control.
4. In AutoCount Cloud, open the invoice and select the configured invoice report.
5. Confirm the report has the user's existing layout, customer, quantities, issued prices, totals, and invoice number.
6. Use Cloud's own Print or Export PDF control.
7. If sharing is needed, use Cloud's own share/export flow; do not send automatically.
8. Return to the app and confirm no second invoice was created.

- [ ] **Step 3: Verify failure and cancellation behavior**

Confirm that a missing or invalid Cloud URL gives a clear non-success message, cancelling a new tab or print dialog leaves the invoice issued, and no e-Invoice submission is triggered. Confirm the browser asset and API responses contain neither account-book IDs nor AutoCount credentials.

- [ ] **Step 4: Update documentation and commit verification evidence**

Add the exact manual steps and observed result to docs/testing/manual-test-checklist.md. Update README status so it says either Cloud report handoff verified or Cloud report handoff blocked, with the reason. Run:

~~~powershell
python -m pytest tests/ -q
git diff --check
git status --short --branch
~~~

Expected: the full suite passes, whitespace validation passes, and the branch status identifies only the intended plan/feature commits.

- [ ] **Step 5: Commit the verification record**

~~~powershell
git add docs/testing/manual-test-checklist.md README.md
git commit -m "docs: verify Cloud report handoff"
~~~

## Completion decision

The branch is PASS only when the user's Cloud report is the printed/exported output and the post-issue actions perform no accounting writes. If AutoCount cannot provide a stable safe handoff, the branch is PARTIAL with the documented generic Cloud/manual-search fallback; it must not claim that the app can directly share an AutoCount-format PDF.
