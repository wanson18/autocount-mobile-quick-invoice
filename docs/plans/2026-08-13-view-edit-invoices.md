# View and Edit Issued Invoices — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator browse recently issued invoices on the mobile page, open one, and add, remove, or re-price its lines.

**Architecture:** Two new read methods on the existing `AutoCountMasterDataAdapter`, a new `InvoiceEditService` for the write path (kept out of the 497-line create service), and a payload builder that sends AutoCount's positional `details` array as complete desired state. Three new endpoints hidden from `/openapi.json` so the Custom GPT never sees them, plus five new mobile screens.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, httpx (async), pytest with `httpx.MockTransport`, vanilla JS single-page frontend.

**Spec:** `docs/specs/2026-08-13-view-edit-invoices-design.md`

## Global Constraints

- **Money is exact.** Quantities and prices are `Decimal` end to end; they serialise to JSON as **bare numbers** via the client's `_encode_json_body`, and to HTTP responses as **strings**. Never `float`. Never a quoted decimal in an AutoCount payload.
- **The account book ID is a server secret.** Clients send a `CompanyKey` only. Always resolve via `get_company(...)`.
- **Fail closed.** A malformed or inconsistent 2xx AutoCount payload raises `AutoCountDataError`. Error messages never include raw response bodies.
- **`accNo` is per-line**, value `"500-0000"` (`DEFAULT_ACC_NO` in `app/autocount/mapping.py`).
- **Every new endpoint is `include_in_schema=False`.** The GPT Action reads `/openapi.json`; these must not appear there.
- **Run `pytest tests/` before every commit.** Baseline is 352 passed, 16 skipped.
- **Commit messages state the root cause, not the symptom** (see `agents.md`).
- **Edit window is 30 days** (`EDIT_WINDOW_DAYS = 30`), measured on `doc_date`.

## File Structure

| File | Responsibility |
|---|---|
| `scripts/spike_invoice_update.py` | **Exists.** Live capability probe. Task 1 runs it. |
| `app/models/master_data.py` | Modify: `InvoiceLineSummary` gains `description`; add response models for the two read endpoints. |
| `app/autocount/adapter.py` | Modify: add `get_invoice`, `list_recent_invoices`; populate `description`. |
| `app/autocount/mapping.py` | Modify: add `map_invoice_update_payload`. |
| `app/models/invoice.py` | Modify: add `InvoiceEditInput`, `InvoiceEditLine`, `ExpectedLine`. |
| `app/services/invoice_edit_service.py` | **Create.** Guards, write, ambiguous-write reconciliation. |
| `app/dependencies.py` | Modify: wire `get_invoice_edit_service`. |
| `app/api/invoices.py` | Modify: add the three endpoints. |
| `app/main.py` | Modify: four exception handlers. |
| `app/static/app.js` | **Create.** Existing inline JS moved verbatim, then extended. |
| `app/static/index.html` | Modify: drop inline `<script>`, add five screens. |
| `tests/unit/test_invoice_read_adapter.py` | **Create.** Adapter reads. |
| `tests/unit/test_invoice_update_mapping.py` | **Create.** Payload builder. |
| `tests/unit/test_invoice_edit_service.py` | **Create.** Guards + reconciliation. |
| `tests/unit/test_invoice_edit_api.py` | **Create.** Endpoint contracts. |
| `docs/autocount/invoice-update-spike.md` | **Create** in Task 1. Live findings. |

---

## Task 1: Live spike — GATE

**This task blocks every other task.** The spike script already exists and its logic is verified against a simulated AutoCount. This task runs it against the real account book and records what came back.

**Files:**
- Run: `scripts/spike_invoice_update.py`
- Create: `docs/autocount/invoice-update-spike.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a go/no-go decision for Tasks 2–11.

- [ ] **Step 1: Supply credentials**

The spike needs live AutoCount credentials. They are **not** in the repo — `.env` is gitignored (`.gitignore:11`), so a fresh clone never has it. Either export them in the shell:

```bash
export AUTOCOUNT_API_KEY_ID=...
export AUTOCOUNT_API_KEY=...
export AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE=...
export AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD=...
```

or, running locally where `.env` exists, `set -a && source .env && set +a`.

`app.config._load` validates the whole company mapping at once and refuses to start if either account book is missing, so **both** account-book variables must be non-empty and distinct even when only one company is spiked. Only Sdn Bhd is live in AutoCount Cloud, so set the Enterprise variable to an unusable placeholder (for example `placeholder-enterprise-not-configured`); it is never used for an HTTP call when probing `sdn_bhd`. Do not relax the guard in `app/config.py` — it exists to stop a misconfigured server mixing data between books.

Alternatively run it from Actions via `.github/workflows/invoice-update-spike.yml`, which applies that placeholder itself and keeps credentials in repository secrets. Note that `workflow_dispatch` only works once the workflow file is on the default branch.

- [ ] **Step 2: Run the spike**

```bash
python scripts/spike_invoice_update.py --company sdn_bhd --confirm
```

This creates ONE throwaway invoice with `saveApprove: true`, PUTs an identical 3-row `details` array, checks the header survived, PUTs a 2-row array, then deletes it. It prints the created document number immediately; if the run dies, delete that invoice by hand or run `python scripts/spike_invoice_update.py --company sdn_bhd --cleanup-only <docNo>`.

- [ ] **Step 3: Read the findings block**

Expected on success:

```
Q1      : YES -- approved invoice accepted PUT (status 204)
Q2      : YES -- every header field survived a PUT with no master key
Q3      : YES -- 3 rows became 2; surviving rows are row1 and row3 as sent.
cleanup : deleted I-0000NN via DELETE /invoice
```

- [ ] **Step 4: GATE — decide whether to continue**

| Finding | Action |
|---|---|
| **Q1 = NO** | **STOP. Tasks 2–11 are void.** Approved invoices cannot be edited via the API, so the whole design fails. Return to the `brainstorming` skill and redesign around void-and-reissue (approach C in the spec). Do not write any code from this plan. |
| **Q2 = NO** | **STOP and report.** Omitting `master` corrupts the header. Task 5's payload builder must instead echo the full existing master back. Revise the spec before continuing. |
| **Q3 = NO** | **Continue, but Task 5 and Task 6 change.** A shorter array does not delete rows, so line removal needs another mechanism. Report before starting Task 5. |
| **All YES** | Continue to Task 2 as written. |

- [ ] **Step 5: Write up the findings**

Create `docs/autocount/invoice-update-spike.md` following the shape of `docs/autocount/pdf-spike.md`: what was asked, what was run, the verbatim findings block, the date, the account book used, and what it means for the design. Paste the findings block verbatim — this file is the citation later tasks point at.

- [ ] **Step 6: Commit**

```bash
git add docs/autocount/invoice-update-spike.md
git commit -m "docs: record live findings for invoice update capability

Answers the three undocumented questions the view-and-edit design rests on,
against the live Sdn Bhd account book."
```

---

## Task 2: Adapter — `get_invoice` and line descriptions

**Files:**
- Modify: `app/models/master_data.py` (`InvoiceLineSummary`)
- Modify: `app/autocount/adapter.py` (`_invoice_detail`, new `get_invoice`)
- Test: `tests/unit/test_invoice_read_adapter.py` (create)

**Interfaces:**
- Consumes: `AutoCountMasterDataAdapter._invoice_row`, `_json_object`, `_validate_id` (all existing private helpers).
- Produces:
  - `InvoiceLineSummary(product_code: str, qty: Decimal, unit_price: Decimal, description: str = "")`
  - `AutoCountMasterDataAdapter.get_invoice(company: CompanyConfig, invoice_no: str) -> InvoiceSummary`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_invoice_read_adapter.py`:

```python
"""Invoice read adapter contract: get_invoice and list_recent_invoices.

``GET /{accountBookId}/invoice?docNo=`` returns the same documented invoice
view model (``master`` + ``details``) as a listing row, so it normalises
through the same path. A mismatched document number fails closed.
"""

import asyncio
from decimal import Decimal

import httpx
import pytest

from app.autocount import AutoCountClient
from app.autocount.adapter import AutoCountMasterDataAdapter
from app.autocount.errors import AutoCountDataError
from app.config import CompanyConfig
from app.models.company import CompanyKey

KEY_ID = "key-id-42"
API_KEY = "api-key-secret-abc-123"
SDN_BHD_AB = "ab-wanson-sdn-bhd-001"

SDN_BHD = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id=SDN_BHD_AB,
)


def make_adapter(handler):
    transport = httpx.MockTransport(handler)
    client = AutoCountClient(KEY_ID, API_KEY, transport=transport)
    return client, AutoCountMasterDataAdapter(client)


def run(client, coro_fn):
    async def _run():
        try:
            return await coro_fn()
        finally:
            await client.aclose()

    return asyncio.run(_run())


def invoice_view(
    *,
    doc_key="9001",
    doc_no="I-000123",
    doc_date="2026-08-13",
    debtor_code="C001",
    total=63.0,
    details=None,
    cancelled=False,
):
    return {
        "master": {
            "docKey": doc_key,
            "docNo": doc_no,
            "docDate": doc_date,
            "debtorCode": debtor_code,
            "total": total,
            "cancelled": cancelled,
        },
        "details": details if details is not None else [
            {
                "productCode": "ITEM-1",
                "description": "Cooking Oil 5kg",
                "qty": 2,
                "unitPrice": 31.5,
            }
        ],
    }


def test_get_invoice_normalises_the_documented_view_model():
    def handler(request):
        assert request.url.path == f"/{SDN_BHD_AB}/invoice"
        assert request.url.params["docNo"] == "I-000123"
        return httpx.Response(200, json=invoice_view())

    client, adapter = make_adapter(handler)
    invoice = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))

    assert invoice.id == "9001"
    assert invoice.doc_no == "I-000123"
    assert invoice.doc_date == "2026-08-13"
    assert invoice.is_cancelled is False
    assert invoice.total == Decimal("63.0")
    assert len(invoice.lines) == 1
    assert invoice.lines[0].product_code == "ITEM-1"
    assert invoice.lines[0].description == "Cooking Oil 5kg"
    assert invoice.lines[0].qty == Decimal("2")
    assert invoice.lines[0].unit_price == Decimal("31.5")


def test_get_invoice_rejects_a_different_document_number():
    def handler(request):
        return httpx.Response(200, json=invoice_view(doc_no="I-999999"))

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError) as exc:
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))
    assert "I-999999" not in str(exc.value)


def test_get_invoice_missing_description_is_blank_not_an_error():
    def handler(request):
        return httpx.Response(
            200,
            json=invoice_view(
                details=[{"productCode": "ITEM-1", "qty": 1, "unitPrice": 10}]
            ),
        )

    client, adapter = make_adapter(handler)
    invoice = run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))
    assert invoice.lines[0].description == ""


def test_get_invoice_rejects_a_malformed_description():
    def handler(request):
        return httpx.Response(
            200,
            json=invoice_view(
                details=[
                    {
                        "productCode": "ITEM-1",
                        "description": {"unexpected": "object"},
                        "qty": 1,
                        "unitPrice": 10,
                    }
                ]
            ),
        )

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_rejects_a_non_object_payload():
    def handler(request):
        return httpx.Response(200, json=["not", "an", "object"])

    client, adapter = make_adapter(handler)
    with pytest.raises(AutoCountDataError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "I-000123"))


def test_get_invoice_rejects_a_blank_document_number():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call should be made")

    client, adapter = make_adapter(handler)
    with pytest.raises(ValueError):
        run(client, lambda: adapter.get_invoice(SDN_BHD, "  "))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_read_adapter.py -v`
Expected: FAIL — `AttributeError: 'AutoCountMasterDataAdapter' object has no attribute 'get_invoice'`

- [ ] **Step 3: Add `description` to `InvoiceLineSummary`**

In `app/models/master_data.py`, replace the `InvoiceLineSummary` dataclass:

```python
@dataclass(frozen=True)
class InvoiceLineSummary:
    """One detail line of an existing AutoCount invoice.

    ``description`` is what AutoCount stored on the line, kept so an edit
    screen can show product names without one master-data call per line. It
    defaults to blank because the listing rows used for price history and
    reconciliation match on code, quantity, and price only.
    """

    product_code: str
    qty: Decimal
    unit_price: Decimal
    description: str = ""
```

- [ ] **Step 4: Populate `description` in the adapter**

In `app/autocount/adapter.py`, replace `_invoice_detail`:

```python
    @classmethod
    def _invoice_detail(cls, detail: Any) -> InvoiceLineSummary:
        if not isinstance(detail, dict):
            raise AutoCountDataError("AutoCount invoice detail row is malformed")
        return InvoiceLineSummary(
            product_code=cls._pick(detail, "productCode", "ProductCode", "product code"),
            qty=cls._strict_decimal(
                detail.get("qty"), "invoice quantity", must_be_positive=True
            ),
            unit_price=cls._strict_decimal(
                detail.get("unitPrice"), "invoice unit price", must_be_positive=False
            ),
            description=cls._invoice_description(detail),
        )

    @staticmethod
    def _invoice_description(detail: dict[str, Any]) -> str:
        """The line's stored description, blank when absent.

        Unlike the identifying fields this is presentational, so an absent
        value is normal rather than malformed; a non-string is still a
        contract violation and fails closed.
        """
        raw = detail.get("description")
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise AutoCountDataError("AutoCount invoice detail description is malformed")
        return raw.strip()
```

- [ ] **Step 5: Add `get_invoice`**

In `app/autocount/adapter.py`, add after `search_invoices`:

```python
    async def get_invoice(
        self, company: CompanyConfig, invoice_no: str
    ) -> InvoiceSummary:
        """One invoice by its AutoCount document number.

        ``GET /invoice?docNo=`` returns the same documented view model
        (``master`` + ``details``) as an invoice listing row, so it
        normalises through ``_invoice_row`` unchanged. The returned ``docNo``
        must match the requested one; a mismatch fails closed rather than
        silently returning a different invoice.

        Never cached: the edit path's stale-write guard and ambiguous-write
        reconciliation both depend on this reflecting AutoCount right now.
        """
        invoice_no = self._validate_id(invoice_no, "invoice_no")
        response = await self._client.read(
            company, "GET", "invoice", params={"docNo": invoice_no}
        )
        payload = self._json_object(
            response, "AutoCount returned a malformed invoice payload"
        )
        invoice = self._invoice_row(payload)
        if invoice.doc_no != invoice_no:
            raise AutoCountDataError(
                "AutoCount returned a different invoice for the requested document number"
            )
        return invoice
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_read_adapter.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 358 passed, 16 skipped — the 352 baseline plus 6 new. If any existing test fails, `description` was made required somewhere; it must stay defaulted.

- [ ] **Step 8: Commit**

```bash
git add app/models/master_data.py app/autocount/adapter.py tests/unit/test_invoice_read_adapter.py
git commit -m "feat: read one invoice by document number

The edit path needs current invoice state for display, the stale-write
guard, and ambiguous-write reconciliation. Get Invoice returns the same view
model as a listing row, so it reuses the existing normalisation; a mismatched
docNo fails closed. Lines now carry their stored description so the edit
screen needs no per-line master-data call."
```

---

## Task 3: Adapter — `list_recent_invoices`

**Files:**
- Modify: `app/autocount/adapter.py`
- Test: `tests/unit/test_invoice_read_adapter.py` (append)

**Interfaces:**
- Consumes: `_listing`, `_invoice_row` from Task 2's file.
- Produces: `AutoCountMasterDataAdapter.list_recent_invoices(company: CompanyConfig, *, date_from: str, date_to: str) -> list[InvoiceSummary]`, sorted newest first.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_invoice_read_adapter.py`:

```python
def listing_payload(*rows):
    return {"data": list(rows), "totalCount": len(rows)}


def test_list_recent_invoices_filters_on_document_date():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=listing_payload(invoice_view()))

    client, adapter = make_adapter(handler)
    run(
        client,
        lambda: adapter.list_recent_invoices(
            SDN_BHD, date_from="2026-07-14", date_to="2026-08-13"
        ),
    )

    import json as _json

    body = _json.loads(captured[0].content)
    assert captured[0].url.path == f"/{SDN_BHD_AB}/invoice/listing"
    assert body["filter"] == {"date": {"from": "2026-07-14", "to": "2026-08-13"}}
    assert body["page"] == 1
    assert "debtorCode" not in body["filter"]


def test_list_recent_invoices_returns_newest_first():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", doc_date="2026-08-01"),
        invoice_view(doc_key="3", doc_no="I-000003", doc_date="2026-08-13"),
        invoice_view(doc_key="2", doc_no="I-000002", doc_date="2026-08-07"),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(
        client,
        lambda: adapter.list_recent_invoices(
            SDN_BHD, date_from="2026-07-14", date_to="2026-08-13"
        ),
    )

    assert [i.doc_no for i in invoices] == ["I-000003", "I-000002", "I-000001"]


def test_list_recent_invoices_breaks_same_day_ties_on_document_number():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", doc_date="2026-08-13"),
        invoice_view(doc_key="2", doc_no="I-000002", doc_date="2026-08-13"),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(
        client,
        lambda: adapter.list_recent_invoices(
            SDN_BHD, date_from="2026-07-14", date_to="2026-08-13"
        ),
    )

    assert [i.doc_no for i in invoices] == ["I-000002", "I-000001"]


def test_list_recent_invoices_includes_cancelled_invoices():
    rows = [
        invoice_view(doc_key="1", doc_no="I-000001", doc_date="2026-08-13", cancelled=True),
    ]

    def handler(request):
        return httpx.Response(200, json=listing_payload(*rows))

    client, adapter = make_adapter(handler)
    invoices = run(
        client,
        lambda: adapter.list_recent_invoices(
            SDN_BHD, date_from="2026-07-14", date_to="2026-08-13"
        ),
    )

    assert len(invoices) == 1
    assert invoices[0].is_cancelled is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_read_adapter.py -k recent -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_recent_invoices'`

- [ ] **Step 3: Implement**

In `app/autocount/adapter.py`, add after `get_invoice`:

```python
    async def list_recent_invoices(
        self,
        company: CompanyConfig,
        *,
        date_from: str,
        date_to: str,
    ) -> list[InvoiceSummary]:
        """Every invoice in the account book whose document date is in range.

        Uses the documented invoice listing ``date`` (docDate) filter with no
        debtor filter, so the operator sees what they issued regardless of
        customer. ``search_invoices`` keeps its ``debtorCode`` +
        ``createdDate`` filter untouched because price history and
        reconciliation depend on that exact behaviour.

        Sorted newest first, with the document number as a stable tie-break
        for same-day invoices, so the mobile list can render what it receives.
        Cancelled invoices are included and flagged; the caller decides how to
        present them.
        """
        date_from = self._validate_id(date_from, "date_from")
        date_to = self._validate_id(date_to, "date_to")

        def body_for(page: int) -> dict[str, Any]:
            return {
                "page": page,
                "filter": {"date": {"from": date_from, "to": date_to}},
            }

        invoices = await self._listing(
            company,
            "POST",
            "invoice/listing",
            params_for=None,
            body_for=body_for,
            extract=self._invoice_row,
        )
        return sorted(
            invoices, key=lambda i: (i.doc_date, i.doc_no), reverse=True
        )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_read_adapter.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 362 passed, 16 skipped

- [ ] **Step 6: Commit**

```bash
git add app/autocount/adapter.py tests/unit/test_invoice_read_adapter.py
git commit -m "feat: list recent invoices across all customers

Browsing what was just issued needs a docDate-filtered listing with no
debtor filter. search_invoices keeps its debtorCode/createdDate filter
untouched because price history and reconciliation depend on it."
```

---

## Task 4: Read endpoints

**Files:**
- Modify: `app/models/master_data.py` (response models)
- Modify: `app/api/invoices.py`
- Test: `tests/unit/test_invoice_edit_api.py` (create)

**Interfaces:**
- Consumes: `get_invoice`, `list_recent_invoices` from Tasks 2–3.
- Produces:
  - `GET /api/{company}/invoices?days=30` → `InvoiceListResponse`
  - `GET /api/{company}/invoices/{doc_no}` → `InvoiceDetailResponse`
  - `InvoiceListItem(id, doc_no, doc_date, debtor_code, total: str, is_cancelled, line_count)`
  - `InvoiceDetailItem(id, doc_no, doc_date, debtor_code, total: str, is_cancelled, is_editable, lines: list[InvoiceLineItem])`
  - `InvoiceLineItem(product_code, description, quantity: str, unit_price: str)`

- [ ] **Step 1: Add the response models**

In `app/models/master_data.py`, append:

```python
class InvoiceLineItem(BaseModel):
    product_code: str
    description: str
    quantity: str
    unit_price: str


class InvoiceListItem(BaseModel):
    id: str
    doc_no: str
    doc_date: str
    debtor_code: str
    total: str
    is_cancelled: bool
    line_count: int


InvoiceListResponse = ListResponse[InvoiceListItem]


class InvoiceDetailItem(BaseModel):
    id: str
    doc_no: str
    doc_date: str
    debtor_code: str
    total: str
    is_cancelled: bool
    is_editable: bool
    lines: list[InvoiceLineItem]


InvoiceDetailResponse = ItemResponse[InvoiceDetailItem]
```

`app/models/master_data.py` currently imports only `ListResponse`; change the import to:

```python
from app.models.common import ItemResponse, ListResponse
```

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_invoice_edit_api.py`:

```python
"""View-and-edit endpoint contracts.

These endpoints are deliberately absent from /openapi.json: the Custom GPT
Action reads that schema, and editing a live invoice by chat is out of scope.
Money always serialises as an exact decimal string.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_master_data
from app.main import app
from app.models.master_data import InvoiceLineSummary, InvoiceSummary

TODAY = "2026-08-13"


def invoice(
    *,
    doc_key="9001",
    doc_no="I-000123",
    doc_date=TODAY,
    cancelled=False,
    lines=None,
):
    return InvoiceSummary(
        id=doc_key,
        doc_no=doc_no,
        doc_date=doc_date,
        debtor_code="C001",
        total=Decimal("63.00"),
        lines=tuple(
            lines
            if lines is not None
            else [
                InvoiceLineSummary(
                    product_code="ITEM-1",
                    qty=Decimal("2"),
                    unit_price=Decimal("31.50"),
                    description="Cooking Oil 5kg",
                )
            ]
        ),
        is_cancelled=cancelled,
    )


class FakeMasterData:
    def __init__(self, invoices=None):
        self.invoices = invoices if invoices is not None else [invoice()]
        self.list_calls = []

    async def list_recent_invoices(self, company, *, date_from, date_to):
        self.list_calls.append((company.key.value, date_from, date_to))
        return list(self.invoices)

    async def get_invoice(self, company, invoice_no):
        for inv in self.invoices:
            if inv.doc_no == invoice_no:
                return inv
        from app.autocount.errors import AutoCountRejectedError

        raise AutoCountRejectedError(404, "not found")


@pytest.fixture
def client_with(monkeypatch):
    def _build(master):
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", "ab-ent")
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", "ab-sdn")
        app.dependency_overrides[get_master_data] = lambda: master
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


def test_list_invoices_returns_exact_string_money(client_with):
    client = client_with(FakeMasterData())
    response = client.get("/api/sdn_bhd/invoices")

    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["doc_no"] == "I-000123"
    assert row["total"] == "63.00"
    assert isinstance(row["total"], str)
    assert row["line_count"] == 1
    assert row["is_cancelled"] is False


def test_list_invoices_rejects_a_days_window_over_thirty(client_with):
    client = client_with(FakeMasterData())
    assert client.get("/api/sdn_bhd/invoices?days=31").status_code == 422
    assert client.get("/api/sdn_bhd/invoices?days=0").status_code == 422


def test_get_invoice_marks_a_recent_uncancelled_invoice_editable(client_with):
    client = client_with(FakeMasterData())
    response = client.get("/api/sdn_bhd/invoices/I-000123")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_editable"] is True
    assert data["lines"][0] == {
        "product_code": "ITEM-1",
        "description": "Cooking Oil 5kg",
        "quantity": "2",
        "unit_price": "31.50",
    }


def test_get_invoice_marks_a_cancelled_invoice_not_editable(client_with):
    client = client_with(FakeMasterData([invoice(cancelled=True)]))
    response = client.get("/api/sdn_bhd/invoices/I-000123")
    assert response.json()["data"]["is_editable"] is False


def test_get_invoice_marks_an_old_invoice_not_editable(client_with):
    client = client_with(FakeMasterData([invoice(doc_date="2020-01-01")]))
    response = client.get("/api/sdn_bhd/invoices/I-000123")
    assert response.json()["data"]["is_editable"] is False


def test_get_unknown_invoice_is_404(client_with):
    client = client_with(FakeMasterData([]))
    response = client.get("/api/sdn_bhd/invoices/I-999999")
    assert response.status_code == 404
    assert response.json()["error"] == "invoice_not_found"


def test_view_and_edit_endpoints_are_absent_from_the_gpt_schema(client_with):
    client = client_with(FakeMasterData())
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/{company}/invoices" not in paths
    assert "/api/{company}/invoices/{doc_no}" not in paths
    # The create endpoint the GPT does use must still be there.
    assert "/api/invoices" in paths
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_edit_api.py -v`
Expected: FAIL — 404s, because the routes do not exist.

- [ ] **Step 4: Implement the endpoints**

In `app/api/invoices.py`, add these imports:

```python
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query

from app.autocount.errors import AutoCountRejectedError
from app.models.master_data import (
    InvoiceDetailItem,
    InvoiceDetailResponse,
    InvoiceLineItem,
    InvoiceListItem,
    InvoiceListResponse,
)
from app.services.invoice_edit_service import (
    EDIT_WINDOW_DAYS,
    InvoiceNotFoundError,
    is_editable,
)
```

> `EDIT_WINDOW_DAYS`, `is_editable`, and `InvoiceNotFoundError` land in Task 6. To keep this task independently testable, create `app/services/invoice_edit_service.py` now containing **only** those three, and Task 6 adds the service class to the same file:

```python
"""Edit the line set of an already-issued AutoCount invoice."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.master_data import InvoiceSummary

#: How recent an invoice must be to remain editable in this app. Older
#: invoices are view-only and are corrected in AutoCount directly, which
#: keeps a mistyped edit from reaching a reconciled accounting period.
EDIT_WINDOW_DAYS = 30


class InvoiceEditError(Exception):
    """Base error for the invoice edit path."""


class InvoiceNotFoundError(InvoiceEditError):
    """AutoCount has no invoice with the requested document number."""


def is_editable(invoice: InvoiceSummary, *, today: date | None = None) -> bool:
    """Whether this app will edit ``invoice``.

    Cancelled invoices are never editable. Neither is one whose document date
    is more than ``EDIT_WINDOW_DAYS`` old, or dated in the future (which means
    the date is wrong, not that the invoice is fresh).
    """
    if invoice.is_cancelled:
        return False
    today = today or date.today()
    try:
        doc_date = date.fromisoformat(invoice.doc_date[:10])
    except ValueError:
        return False
    if doc_date > today:
        return False
    return (today - doc_date) <= timedelta(days=EDIT_WINDOW_DAYS)
```

Then append the endpoints to `app/api/invoices.py`:

```python
@router.get(
    "/{company}/invoices",
    response_model=InvoiceListResponse,
    include_in_schema=False,
)
async def list_invoices(
    company: CompanyKey,
    days: int = Query(default=EDIT_WINDOW_DAYS, ge=1, le=EDIT_WINDOW_DAYS),
    master=Depends(get_master_data),
) -> InvoiceListResponse:
    """Recent invoices for the selected company, newest first.

    Hidden from the OpenAPI schema on purpose: the Custom GPT Action reads
    that schema, and viewing/editing invoices is a mobile-page-only workflow.
    """
    today = date.today()
    invoices = await master.list_recent_invoices(
        get_company(company),
        date_from=(today - timedelta(days=days)).isoformat(),
        date_to=today.isoformat(),
    )
    return InvoiceListResponse(
        data=[
            InvoiceListItem(
                id=invoice.id,
                doc_no=invoice.doc_no,
                doc_date=invoice.doc_date,
                debtor_code=invoice.debtor_code,
                total=str(invoice.total),
                is_cancelled=invoice.is_cancelled,
                line_count=len(invoice.lines),
            )
            for invoice in invoices
        ]
    )


@router.get(
    "/{company}/invoices/{doc_no}",
    response_model=InvoiceDetailResponse,
    include_in_schema=False,
)
async def get_invoice_detail(
    company: CompanyKey,
    doc_no: str,
    master=Depends(get_master_data),
) -> InvoiceDetailResponse:
    invoice = await _read_invoice(master, get_company(company), doc_no)
    return InvoiceDetailResponse(data=_detail_item(invoice))


async def _read_invoice(master, company, doc_no: str):
    """Fetch one invoice, turning AutoCount's 404 into a domain error.

    Any other upstream status keeps propagating as ``AutoCountRejectedError``
    so it still surfaces as a sanitised 502.
    """
    try:
        return await master.get_invoice(company, doc_no)
    except AutoCountRejectedError as exc:
        if exc.status_code == 404:
            raise InvoiceNotFoundError(
                f"no invoice {doc_no!r} in the selected company"
            ) from None
        raise


def _detail_item(invoice) -> InvoiceDetailItem:
    return InvoiceDetailItem(
        id=invoice.id,
        doc_no=invoice.doc_no,
        doc_date=invoice.doc_date,
        debtor_code=invoice.debtor_code,
        total=str(invoice.total),
        is_cancelled=invoice.is_cancelled,
        is_editable=is_editable(invoice),
        lines=[
            InvoiceLineItem(
                product_code=line.product_code,
                description=line.description,
                quantity=str(line.qty),
                unit_price=str(line.unit_price),
            )
            for line in invoice.lines
        ],
    )
```

- [ ] **Step 5: Add the not-found handler**

In `app/main.py`, import `InvoiceNotFoundError` from `app.services.invoice_edit_service` and add:

```python
@app.exception_handler(InvoiceNotFoundError)
async def invoice_not_found_error_handler(
    request: Request, exc: InvoiceNotFoundError
) -> JSONResponse:
    return _error(404, "invoice_not_found", str(exc))
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_edit_api.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 369 passed, 16 skipped

- [ ] **Step 8: Commit**

```bash
git add app/models/master_data.py app/api/invoices.py app/services/invoice_edit_service.py app/main.py tests/unit/test_invoice_edit_api.py
git commit -m "feat: expose recent-invoice list and detail endpoints

Both hidden from /openapi.json so the Custom GPT Action never sees them;
viewing and editing invoices is a mobile-page-only workflow. AutoCount's 404
becomes a domain error so an unknown docNo is a clean 404 rather than a 502."
```

---

## Task 5: Update payload builder

**Files:**
- Modify: `app/autocount/mapping.py`
- Test: `tests/unit/test_invoice_update_mapping.py` (create)

**Interfaces:**
- Consumes: `DEFAULT_ACC_NO`, `ProductSummary`.
- Produces: `map_invoice_update_payload(lines: Sequence[InvoiceEditLine], products: Mapping[str, ProductSummary], master: Mapping[str, Any]) -> dict[str, Any]`

> **Updated by the spike (2026-08-13).** Q1 and Q3 came back YES, so
> full-state replace stands. Q2 came back NO in a way that changes this task:
> `master` **cannot** be omitted — the API answers
> `400 The Master field is required.` The builder now takes the invoice's
> current master and echoes the five mandatory fields (`docDate`,
> `debtorCode`, `debtorName`, `creditTerm`, `salesLocation`) verbatim,
> leaving optional header fields unsent (confirmed live: they are preserved,
> not blanked). `docDate` arrives as a datetime — echo it unchanged.
> See [`docs/autocount/invoice-update-spike.md`](../autocount/invoice-update-spike.md).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_invoice_update_mapping.py`:

```python
"""Update payload contract for AutoCount's positional details array.

Every edit sends the complete desired line set: AutoCount rewrites detail
rows by position and deletes any row beyond the end of the array, so absolute
desired state is the only encoding that is safe to send twice.
"""

import json
from decimal import Decimal

import pytest

from app.autocount.client import _encode_json_body
from app.autocount.mapping import DEFAULT_ACC_NO, map_invoice_update_payload
from app.models.invoice import InvoiceEditLine
from app.models.master_data import ProductSummary

OIL = ProductSummary("ITEM-1", "ITEM-1", "Cooking Oil 5kg", Decimal("31.50"))
RICE = ProductSummary("ITEM-2", "ITEM-2", "Rice 10kg", Decimal("42.00"))
PRODUCTS = {"ITEM-1": OIL, "ITEM-2": RICE}


def line(item_id, qty, price):
    return InvoiceEditLine(
        item_id=item_id, quantity=Decimal(qty), unit_price=Decimal(price)
    )


def test_payload_carries_only_details():
    payload = map_invoice_update_payload([line("ITEM-1", "2", "31.50")], PRODUCTS)

    assert set(payload) == {"details"}
    assert "master" not in payload
    assert "saveApprove" not in payload


def test_every_row_is_fully_specified_in_order():
    payload = map_invoice_update_payload(
        [line("ITEM-2", "1", "42.00"), line("ITEM-1", "3", "30.00")], PRODUCTS
    )

    assert payload["details"] == [
        {
            "productCode": "ITEM-2",
            "description": "Rice 10kg",
            "qty": Decimal("1"),
            "unitPrice": Decimal("42.00"),
            "accNo": DEFAULT_ACC_NO,
        },
        {
            "productCode": "ITEM-1",
            "description": "Cooking Oil 5kg",
            "qty": Decimal("3"),
            "unitPrice": Decimal("30.00"),
            "accNo": DEFAULT_ACC_NO,
        },
    ]


def test_no_empty_placeholder_rows_are_ever_emitted():
    payload = map_invoice_update_payload(
        [line("ITEM-1", "1", "1.00"), line("ITEM-2", "1", "2.00")], PRODUCTS
    )
    assert all(row != {} for row in payload["details"])
    assert all(set(row) >= {"productCode", "qty", "unitPrice", "accNo"} for row in payload["details"])


def test_quantities_and_prices_stay_decimal_never_float():
    payload = map_invoice_update_payload([line("ITEM-1", "2.5", "31.55")], PRODUCTS)
    row = payload["details"][0]

    assert isinstance(row["qty"], Decimal)
    assert isinstance(row["unitPrice"], Decimal)
    assert not isinstance(row["qty"], float)


def test_encoded_body_sends_bare_json_numbers():
    payload = map_invoice_update_payload([line("ITEM-1", "2.5", "31.55")], PRODUCTS)
    text = _encode_json_body(payload).decode()

    assert '"qty":2.5' in text
    assert '"unitPrice":31.55' in text
    assert '"qty":"' not in text
    assert json.loads(text)["details"][0]["unitPrice"] == 31.55


def test_an_unresolved_product_is_rejected():
    with pytest.raises(ValueError, match="ITEM-9"):
        map_invoice_update_payload([line("ITEM-9", "1", "1.00")], PRODUCTS)


def test_a_mismatched_product_identity_is_rejected():
    wrong = {"ITEM-1": ProductSummary("ITEM-1", "OTHER", "Wrong", Decimal("1"))}
    with pytest.raises(ValueError):
        map_invoice_update_payload([line("ITEM-1", "1", "1.00")], wrong)


def test_an_empty_line_set_is_rejected():
    with pytest.raises(ValueError, match="at least one line"):
        map_invoice_update_payload([], PRODUCTS)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_update_mapping.py -v`
Expected: FAIL — `ImportError: cannot import name 'map_invoice_update_payload'`

(`InvoiceEditLine` also does not exist yet; Step 3 adds it.)

- [ ] **Step 3: Add the input models**

In `app/models/invoice.py`, append:

```python
class InvoiceEditLine(BaseModel):
    """One line of the desired post-edit invoice.

    ``item_id`` is AutoCount's product lookup code, the same string as
    ``ProductSummary.code`` and the payload's ``productCode``.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: NonBlankIdentifier
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class ExpectedLine(BaseModel):
    """One line of the invoice as the client loaded it, for the stale guard."""

    model_config = ConfigDict(extra="forbid")

    item_id: NonBlankIdentifier
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class InvoiceEditInput(BaseModel):
    """A confirmed edit to one issued invoice.

    ``lines`` is the complete desired line set, not a diff: AutoCount rewrites
    detail rows by position and drops any row past the end of the array.
    ``expected_lines`` is what the client had on screen, used to reject a save
    that would clobber a change made in AutoCount in the meantime. The
    document number comes from the URL path, never the body.
    """

    model_config = ConfigDict(extra="forbid")

    company: CompanyKey
    expected_lines: list[ExpectedLine]
    lines: list[InvoiceEditLine] = Field(min_length=1)
```

- [ ] **Step 4: Implement the builder**

In `app/autocount/mapping.py`, add the imports and the function:

```python
from collections.abc import Sequence

from app.models.invoice import InvoiceDraftInput, InvoiceEditLine
```

```python
def map_invoice_update_payload(
    lines: Sequence[InvoiceEditLine],
    products: Mapping[str, ProductSummary],
) -> dict[str, Any]:
    """Build the AutoCount Update Invoice body for a complete desired line set.

    AutoCount's Update Invoice treats ``details`` as a positional array: row
    N of the request overwrites row N of the stored invoice, and any stored
    row past the end of the array is deleted
    (https://accounting-api.autocountcloud.com/documentation/api-methods/invoice/update-invoice/).
    The documented way to leave a row alone is an empty ``{}`` in its slot,
    but this builder never emits one: it always spells out every surviving
    row in full, so removing the middle line of three is just sending rows
    one and three. That makes the request absolute desired state, which is
    what lets a timed-out write be resolved by re-reading instead of guessed
    at.

    ``master`` is deliberately absent so the header -- document date, debtor,
    delivery address, credit term, sales location -- is preserved by
    omission; only fields present in the body are touched. ``saveApprove`` is
    likewise not sent: the invoice is already approved.

    ``description`` comes from the resolved product master, exactly as
    ``map_invoice_payload`` does on create, so an edited invoice carries the
    same descriptions a freshly created one would.
    """
    if not lines:
        raise ValueError("an invoice must keep at least one line")

    details: list[dict[str, Any]] = []
    for line in lines:
        product = products.get(line.item_id)
        if product is None or product.id != line.item_id or product.code != line.item_id:
            raise ValueError(f"resolved product does not match item {line.item_id}")
        details.append(
            {
                "productCode": product.code,
                "description": product.name,
                # Decimal, not str(): app.autocount.client encodes Decimal as
                # an exact bare JSON number. A quoted decimal fails with a
                # System.Decimal conversion error and float() would risk
                # silently rounding a price.
                "qty": line.quantity,
                "unitPrice": line.unit_price,
                "accNo": DEFAULT_ACC_NO,
            }
        )
    return {"details": details}
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_update_mapping.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 377 passed, 16 skipped

- [ ] **Step 7: Commit**

```bash
git add app/autocount/mapping.py app/models/invoice.py tests/unit/test_invoice_update_mapping.py
git commit -m "feat: build update payloads as complete desired line state

AutoCount rewrites detail rows by position and deletes any row past the end
of the array, so a diff with {} placeholders would silently corrupt the wrong
row on an off-by-one and would not be safe to retry. Sending every surviving
row in full makes the request idempotent. master is omitted so the header
survives untouched."
```

---

## Task 6: Edit service

**Files:**
- Modify: `app/services/invoice_edit_service.py` (created in Task 4)
- Test: `tests/unit/test_invoice_edit_service.py` (create)

**Interfaces:**
- Consumes: `map_invoice_update_payload` (Task 5), `get_invoice` (Task 2), `is_editable`/`EDIT_WINDOW_DAYS` (Task 4).
- Produces:
  - `InvoiceEditService(company_resolver, master_data, client).edit(doc_no: str, edit: InvoiceEditInput, *, today: date | None = None) -> InvoiceSummary`
  - Exceptions: `InvoiceNotEditableError`, `InvoiceChangedError`, `InvoiceEditUnconfirmedError`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_invoice_edit_service.py`:

```python
"""Edit-path guards and ambiguous-write resolution.

Two hazards, both handled without idempotency keys: a stale write (the
invoice changed in AutoCount since it was loaded) and an ambiguous write (the
PUT timed out). Because the request is absolute desired state, the second is
decidable by re-reading and comparing.
"""

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from app.autocount.errors import AutoCountAmbiguousWriteError, AutoCountRejectedError
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import ExpectedLine, InvoiceEditInput, InvoiceEditLine
from app.models.master_data import InvoiceLineSummary, InvoiceSummary, ProductSummary
from app.services.invoice_edit_service import (
    InvoiceChangedError,
    InvoiceEditService,
    InvoiceEditUnconfirmedError,
    InvoiceNotEditableError,
    InvoiceNotFoundError,
)

TODAY = date(2026, 8, 13)
SDN_BHD = CompanyConfig(
    key=CompanyKey.SDN_BHD,
    name="Wanson Enterprise (M) Sdn Bhd",
    account_book_id="ab-sdn",
)


def summary(*, doc_date="2026-08-13", cancelled=False, lines=None):
    return InvoiceSummary(
        id="9001",
        doc_no="I-000123",
        doc_date=doc_date,
        debtor_code="C001",
        total=Decimal("63.00"),
        lines=tuple(
            lines
            if lines is not None
            else [
                InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),
            ]
        ),
        is_cancelled=cancelled,
    )


class FakeMasterData:
    def __init__(self, reads):
        self.reads = list(reads)
        self.read_count = 0

    async def get_invoice(self, company, invoice_no):
        self.read_count += 1
        value = self.reads[min(self.read_count - 1, len(self.reads) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_item(self, company, item_id):
        return ProductSummary(item_id, item_id, f"Product {item_id}", Decimal("1"))


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.writes = []

    async def write(self, company, method, endpoint, *, params=None, json=None):
        self.writes.append((method, endpoint, params, json))
        if self.error:
            raise self.error
        return None


def make_service(master, client):
    return InvoiceEditService(
        company_resolver=lambda key: SDN_BHD,
        master_data=master,
        client=client,
    )


def edit_input(lines=None, expected=None):
    return InvoiceEditInput(
        company=CompanyKey.SDN_BHD,
        expected_lines=expected
        if expected is not None
        else [ExpectedLine(item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("31.50"))],
        lines=lines
        if lines is not None
        else [InvoiceEditLine(item_id="ITEM-1", quantity=Decimal("5"), unit_price=Decimal("31.50"))],
    )


def run(coro):
    return asyncio.run(coro)


def test_edit_writes_the_full_desired_line_set():
    after = summary(lines=[InvoiceLineSummary("ITEM-1", Decimal("5"), Decimal("31.50"), "Oil")])
    master = FakeMasterData([summary(), after])
    client = FakeClient()
    service = make_service(master, client)

    result = run(service.edit("I-000123", edit_input(), today=TODAY))

    method, endpoint, params, body = client.writes[0]
    assert (method, endpoint) == ("PUT", "invoice")
    assert params == {"docNo": "I-000123"}
    assert body["details"][0]["qty"] == Decimal("5")
    assert "master" not in body
    assert len(client.writes) == 1
    assert result.lines[0].qty == Decimal("5")


def test_a_cancelled_invoice_is_not_editable():
    master = FakeMasterData([summary(cancelled=True)])
    client = FakeClient()
    service = make_service(master, client)

    with pytest.raises(InvoiceNotEditableError):
        run(service.edit("I-000123", edit_input(), today=TODAY))
    assert client.writes == []


def test_an_invoice_older_than_the_window_is_not_editable():
    master = FakeMasterData([summary(doc_date="2026-06-01")])
    client = FakeClient()
    service = make_service(master, client)

    with pytest.raises(InvoiceNotEditableError):
        run(service.edit("I-000123", edit_input(), today=TODAY))
    assert client.writes == []


def test_an_unknown_invoice_is_not_found():
    master = FakeMasterData([AutoCountRejectedError(404, "not found")])
    client = FakeClient()
    service = make_service(master, client)

    with pytest.raises(InvoiceNotFoundError):
        run(service.edit("I-000123", edit_input(), today=TODAY))
    assert client.writes == []


def test_a_changed_invoice_is_rejected_before_writing():
    changed = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("9"), Decimal("31.50"), "Oil")]
    )
    master = FakeMasterData([changed])
    client = FakeClient()
    service = make_service(master, client)

    with pytest.raises(InvoiceChangedError):
        run(service.edit("I-000123", edit_input(), today=TODAY))
    assert client.writes == []


def test_an_ambiguous_write_that_applied_is_reported_as_success():
    applied = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("5"), Decimal("31.50"), "Oil")]
    )
    master = FakeMasterData([summary(), applied])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))
    service = make_service(master, client)

    result = run(service.edit("I-000123", edit_input(), today=TODAY))
    assert result.lines[0].qty == Decimal("5")


def test_an_ambiguous_write_that_did_not_apply_is_unconfirmed():
    master = FakeMasterData([summary(), summary()])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))
    service = make_service(master, client)

    with pytest.raises(InvoiceEditUnconfirmedError):
        run(service.edit("I-000123", edit_input(), today=TODAY))


def test_an_ambiguous_write_whose_reread_fails_propagates_the_ambiguity():
    master = FakeMasterData([summary(), AutoCountRejectedError(500, "boom")])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))
    service = make_service(master, client)

    with pytest.raises(AutoCountAmbiguousWriteError):
        run(service.edit("I-000123", edit_input(), today=TODAY))


def test_removing_a_line_sends_the_shorter_array():
    two_lines = summary(
        lines=[
            InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),
            InvoiceLineSummary("ITEM-2", Decimal("1"), Decimal("42.00"), "Rice"),
        ]
    )
    after = summary(lines=[InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil")])
    master = FakeMasterData([two_lines, after])
    client = FakeClient()
    service = make_service(master, client)

    run(
        service.edit(
            "I-000123",
            edit_input(
                expected=[
                    ExpectedLine(item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("31.50")),
                    ExpectedLine(item_id="ITEM-2", quantity=Decimal("1"), unit_price=Decimal("42.00")),
                ],
                lines=[
                    InvoiceEditLine(item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("31.50"))
                ],
            ),
            today=TODAY,
        )
    )

    _, _, _, body = client.writes[0]
    assert len(body["details"]) == 1
    assert body["details"][0]["productCode"] == "ITEM-1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_edit_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'InvoiceEditService'`

- [ ] **Step 3: Implement the service**

Append to `app/services/invoice_edit_service.py`:

```python
from collections.abc import Callable
from typing import Any, Protocol

from app.autocount.errors import AutoCountAmbiguousWriteError, AutoCountRejectedError
from app.autocount.mapping import map_invoice_update_payload
from app.config import CompanyConfig
from app.models.company import CompanyKey
from app.models.invoice import InvoiceEditInput
from app.models.master_data import ProductSummary


class InvoiceNotEditableError(InvoiceEditError):
    """The invoice is cancelled or outside the edit window."""


class InvoiceChangedError(InvoiceEditError):
    """The invoice changed in AutoCount since the client loaded it."""


class InvoiceEditUnconfirmedError(InvoiceEditError):
    """A timed-out edit could not be confirmed by re-reading the invoice."""


class EditMasterDataPort(Protocol):
    async def get_invoice(self, company: CompanyConfig, invoice_no: str) -> Any: ...

    async def get_item(
        self, company: CompanyConfig, item_id: str
    ) -> ProductSummary: ...


class EditWritePort(Protocol):
    async def write(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any: ...


class InvoiceEditService:
    """Apply one edited line set to an already-issued AutoCount invoice.

    Deliberately separate from ``InvoiceService``: creation is keyed by an
    idempotency key and needs a request repository because a repeated create
    makes a second invoice. A repeated full-state update does not -- it
    re-asserts the same line array -- so this path stores nothing and its two
    write hazards are handled inline:

    - **Stale write.** The client sends the line set it loaded; the invoice is
      re-read immediately before the update and the edit is refused if it no
      longer matches, rather than silently clobbering a change made in
      AutoCount.
    - **Ambiguous write.** A timeout leaves the update's fate unknown, but
      because the request is absolute desired state the answer is one re-read
      away: the invoice either matches what was asked for or it does not.
    """

    def __init__(
        self,
        *,
        company_resolver: Callable[[CompanyKey], CompanyConfig | None],
        master_data: EditMasterDataPort,
        client: EditWritePort,
    ) -> None:
        self.company_resolver = company_resolver
        self.master_data = master_data
        self.client = client

    async def edit(
        self,
        doc_no: str,
        edit: InvoiceEditInput,
        *,
        today: date | None = None,
    ) -> InvoiceSummary:
        company = self.company_resolver(edit.company)
        if company is None:
            raise InvoiceEditError(f"unknown company: {edit.company.value}")

        current = await self._read(company, doc_no)
        if not is_editable(current, today=today):
            raise InvoiceNotEditableError(
                f"invoice {doc_no} is cancelled or older than {EDIT_WINDOW_DAYS} days "
                "and can only be corrected in AutoCount"
            )
        self._assert_unchanged(current, edit, doc_no)

        products = await self._resolve_products(company, edit)
        payload = map_invoice_update_payload(edit.lines, products)

        try:
            await self.client.write(
                company, "PUT", "invoice", params={"docNo": doc_no}, json=payload
            )
        except AutoCountAmbiguousWriteError as ambiguous:
            return await self._reconcile(company, doc_no, edit, ambiguous)

        return await self._read(company, doc_no)

    async def _read(self, company: CompanyConfig, doc_no: str) -> InvoiceSummary:
        try:
            return await self.master_data.get_invoice(company, doc_no)
        except AutoCountRejectedError as exc:
            if exc.status_code == 404:
                raise InvoiceNotFoundError(
                    f"no invoice {doc_no!r} in the selected company"
                ) from None
            raise

    @staticmethod
    def _assert_unchanged(
        current: InvoiceSummary, edit: InvoiceEditInput, doc_no: str
    ) -> None:
        """Refuse a save built on a stale view of the invoice.

        Compared as an ordered tuple with exact ``Decimal`` equality: row
        order is meaningful to AutoCount, so a reorder is a real change.
        """
        loaded = [
            (line.item_id, line.quantity, line.unit_price)
            for line in edit.expected_lines
        ]
        stored = [
            (line.product_code, line.qty, line.unit_price) for line in current.lines
        ]
        if loaded != stored:
            raise InvoiceChangedError(
                f"invoice {doc_no} changed in AutoCount since it was opened; "
                "reopen it and reapply the edit"
            )

    async def _resolve_products(
        self, company: CompanyConfig, edit: InvoiceEditInput
    ) -> dict[str, ProductSummary]:
        """Resolve every edited line's product from the selected account book.

        Mirrors the create path: an item is only usable if the selected
        company's own book returns it under exactly that code, so an item from
        the other company can never be introduced by an edit.
        """
        products: dict[str, ProductSummary] = {}
        for line in edit.lines:
            if line.item_id in products:
                continue
            product = await self.master_data.get_item(company, line.item_id)
            if product.id != line.item_id or product.code != line.item_id:
                raise InvoiceEditError(
                    f"item {line.item_id!r} does not belong to selected company"
                )
            products[line.item_id] = product
        return products

    async def _reconcile(
        self,
        company: CompanyConfig,
        doc_no: str,
        edit: InvoiceEditInput,
        ambiguous: AutoCountAmbiguousWriteError,
    ) -> InvoiceSummary:
        """Decide whether a timed-out update actually applied.

        The update carried absolute desired state, so the stored invoice
        either matches it or it does not -- no partial application to unpick.
        If the re-read itself fails there is no answer, so the original
        ambiguity propagates and the caller sees the existing retryable 502.
        """
        try:
            current = await self._read(company, doc_no)
        except InvoiceEditError:
            raise ambiguous from None
        except AutoCountRejectedError:
            raise ambiguous from None

        desired = [
            (line.item_id, line.quantity, line.unit_price) for line in edit.lines
        ]
        stored = [
            (line.product_code, line.qty, line.unit_price) for line in current.lines
        ]
        if desired == stored:
            return current
        raise InvoiceEditUnconfirmedError(
            f"the edit to invoice {doc_no} timed out and could not be confirmed; "
            "reopen the invoice to check its current state before retrying"
        )
```

Add `InvoiceSummary` to the existing `app.models.master_data` import at the top of the file.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_edit_service.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 386 passed, 16 skipped

- [ ] **Step 6: Commit**

```bash
git add app/services/invoice_edit_service.py tests/unit/test_invoice_edit_service.py
git commit -m "feat: apply invoice edits with stale and ambiguous write guards

Kept out of InvoiceService: creation needs an idempotency key and a request
repository because a repeated create makes a second invoice, while a repeated
full-state update re-asserts the same lines and stores nothing. A stale view
is refused before writing; a timed-out write is resolved by re-reading, which
is only decidable because the request is absolute desired state."
```

---

## Task 7: `PUT` endpoint and error handlers

**Files:**
- Modify: `app/dependencies.py`
- Modify: `app/api/invoices.py`
- Modify: `app/main.py`
- Test: `tests/unit/test_invoice_edit_api.py` (append)

**Interfaces:**
- Consumes: `InvoiceEditService` (Task 6).
- Produces: `PUT /api/{company}/invoices/{doc_no}` → `InvoiceDetailResponse`; `get_invoice_edit_service` dependency.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_invoice_edit_api.py`:

```python
from app.dependencies import get_invoice_edit_service
from app.services.invoice_edit_service import (
    InvoiceChangedError,
    InvoiceEditUnconfirmedError,
    InvoiceNotEditableError,
)


class FakeEditService:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else invoice()
        self.error = error
        self.calls = []

    async def edit(self, doc_no, edit, *, today=None):
        self.calls.append((doc_no, edit))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def edit_client(monkeypatch):
    def _build(service):
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE", "ab-ent")
        monkeypatch.setenv("AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD", "ab-sdn")
        app.dependency_overrides[get_invoice_edit_service] = lambda: service
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


VALID_BODY = {
    "company": "sdn_bhd",
    "expected_lines": [
        {"item_id": "ITEM-1", "quantity": "2", "unit_price": "31.50"}
    ],
    "lines": [{"item_id": "ITEM-1", "quantity": "5", "unit_price": "31.50"}],
}


def test_put_returns_the_updated_invoice(edit_client):
    service = FakeEditService()
    client = edit_client(service)

    response = client.put("/api/sdn_bhd/invoices/I-000123", json=VALID_BODY)

    assert response.status_code == 200
    assert response.json()["data"]["doc_no"] == "I-000123"
    assert service.calls[0][0] == "I-000123"


def test_put_rejects_an_empty_line_set(edit_client):
    client = edit_client(FakeEditService())
    body = dict(VALID_BODY, lines=[])
    assert client.put("/api/sdn_bhd/invoices/I-000123", json=body).status_code == 422


def test_put_rejects_unknown_body_fields(edit_client):
    client = edit_client(FakeEditService())
    body = dict(VALID_BODY, invoice_date="2026-08-13")
    assert client.put("/api/sdn_bhd/invoices/I-000123", json=body).status_code == 422


@pytest.mark.parametrize(
    "error,status,code",
    [
        (InvoiceNotEditableError("locked"), 409, "invoice_not_editable"),
        (InvoiceChangedError("changed"), 409, "invoice_changed"),
        (InvoiceEditUnconfirmedError("unconfirmed"), 409, "edit_unconfirmed"),
    ],
)
def test_edit_failures_map_to_structured_errors(edit_client, error, status, code):
    client = edit_client(FakeEditService(error=error))
    response = client.put("/api/sdn_bhd/invoices/I-000123", json=VALID_BODY)

    assert response.status_code == status
    assert response.json()["error"] == code


def test_the_put_endpoint_is_absent_from_the_gpt_schema(edit_client):
    client = edit_client(FakeEditService())
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/{company}/invoices/{doc_no}" not in paths
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_invoice_edit_api.py -k put -v`
Expected: FAIL — 405 Method Not Allowed.

- [ ] **Step 3: Wire the dependency**

In `app/dependencies.py`, add:

```python
from app.services.invoice_edit_service import InvoiceEditService

_edit_service_instance: InvoiceEditService | None = None


def get_invoice_edit_service() -> InvoiceEditService:
    global _edit_service_instance
    if _edit_service_instance is None:
        _edit_service_instance = InvoiceEditService(
            company_resolver=get_company,
            master_data=get_master_data(),
            client=_get_client(),
        )
    return _edit_service_instance
```

- [ ] **Step 4: Add the endpoint**

In `app/api/invoices.py`, add `from app.models.invoice import InvoiceEditInput` and `from app.dependencies import get_invoice_edit_service` to the imports, then:

```python
@router.put(
    "/{company}/invoices/{doc_no}",
    response_model=InvoiceDetailResponse,
    include_in_schema=False,
)
async def update_invoice(
    company: CompanyKey,
    doc_no: str,
    edit: InvoiceEditInput,
    service=Depends(get_invoice_edit_service),
) -> InvoiceDetailResponse:
    """Replace an issued invoice's line set with the confirmed desired state.

    Hidden from the OpenAPI schema: editing a live invoice is a mobile-page
    workflow, never a Custom GPT action.
    """
    updated = await service.edit(doc_no, edit)
    return InvoiceDetailResponse(data=_detail_item(updated))
```

- [ ] **Step 5: Add the error handlers**

In `app/main.py`, extend the `app.services.invoice_edit_service` import to include `InvoiceChangedError`, `InvoiceEditError`, `InvoiceEditUnconfirmedError`, `InvoiceNotEditableError`, then add:

```python
@app.exception_handler(InvoiceNotEditableError)
async def invoice_not_editable_error_handler(
    request: Request, exc: InvoiceNotEditableError
) -> JSONResponse:
    return _error(409, "invoice_not_editable", str(exc))


@app.exception_handler(InvoiceChangedError)
async def invoice_changed_error_handler(
    request: Request, exc: InvoiceChangedError
) -> JSONResponse:
    return _error(409, "invoice_changed", str(exc))


@app.exception_handler(InvoiceEditUnconfirmedError)
async def invoice_edit_unconfirmed_error_handler(
    request: Request, exc: InvoiceEditUnconfirmedError
) -> JSONResponse:
    return _error(409, "edit_unconfirmed", str(exc))


@app.exception_handler(InvoiceEditError)
async def invoice_edit_error_handler(
    request: Request, exc: InvoiceEditError
) -> JSONResponse:
    return _error(400, "invalid_invoice", str(exc))
```

> Handler order matters: FastAPI dispatches on the exact exception class, and the subclasses are registered before the `InvoiceEditError` base, so each keeps its own status.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_invoice_edit_api.py -v`
Expected: PASS (14 tests)

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 393 passed, 16 skipped

- [ ] **Step 8: Commit**

```bash
git add app/dependencies.py app/api/invoices.py app/main.py tests/unit/test_invoice_edit_api.py
git commit -m "feat: expose the invoice edit endpoint

Hidden from /openapi.json like the read endpoints so the Custom GPT Action
cannot edit a live invoice. Each edit failure maps to its own structured
error so the mobile page can tell 'reopen it' from 'this is locked'."
```

---

## Task 8: Extract the frontend JavaScript

Pure refactor: no behaviour change, so the deployed page must work identically before and after. Doing this first keeps Tasks 9–10 from editing an 1100-line HTML file.

**Files:**
- Create: `app/static/app.js`
- Modify: `app/static/index.html`

**Interfaces:**
- Consumes: nothing.
- Produces: `app/static/app.js` holding the existing IIFE verbatim.

- [ ] **Step 1: Move the script**

Cut everything between `<script>` and `</script>` in `app/static/index.html` (starting at the `(function () {` on line ~282) into a new `app/static/app.js`. Do not change a single line of the JavaScript.

- [ ] **Step 2: Reference it from the page**

Replace the now-empty `<script>...</script>` block in `index.html` with:

```html
<script src="/app.js"></script>
```

`StaticFiles` is mounted at `/` in `app/main.py`, so `/app.js` resolves to `app/static/app.js` with no routing change.

- [ ] **Step 3: Verify the page still works**

```bash
python -m uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/`, then confirm in the browser devtools console that `/app.js` returns 200 and no errors appear. Walk company → customer → items far enough to see the item search return results.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: 393 passed, 16 skipped (unchanged — no Python touched)

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html app/static/app.js
git commit -m "refactor: move the mobile page's inline script to app.js

index.html was 832 lines with all JavaScript inline; the view-and-edit
screens would push it past 1300. Moved verbatim with no behaviour change so
the new screens land in a file that can be read in one sitting."
```

---

## Task 9: View screens

**Files:**
- Modify: `app/static/index.html` (markup)
- Modify: `app/static/app.js` (behaviour)

**Interfaces:**
- Consumes: `GET /api/{company}/invoices`, `GET /api/{company}/invoices/{doc_no}` (Task 4).
- Produces: `mode`, `invoiceList`, `invoiceDetail` screens; `state.viewInvoice` holding the loaded detail payload.

- [ ] **Step 1: Add the markup**

In `app/static/index.html`, after the `company` section and before the `customer` section, add:

```html
<section class="screen" data-screen="mode">
  <div class="selected-summary" id="mode-context"></div>
  <div class="company-grid">
    <button type="button" class="company-btn" id="mode-new">New invoice</button>
    <button type="button" class="company-btn" id="mode-view">Recent invoices</button>
  </div>
</section>

<section class="screen" data-screen="invoiceList">
  <div class="selected-summary" id="invoice-list-context"></div>
  <div class="list" id="invoice-list"></div>
</section>

<section class="screen" data-screen="invoiceDetail">
  <div class="status-card" id="invoice-detail-card"></div>
  <div id="invoice-detail-lines"></div>
  <div class="review-total">
    <span>Total</span>
    <span id="invoice-detail-total">RM 0.00</span>
  </div>
</section>
```

- [ ] **Step 2: Add the screens to the flow**

In `app/static/app.js`, extend the `STEPS` array so `mode` follows `company`, and add `invoiceList` and `invoiceDetail` as screens reachable from `mode` rather than as linear wizard steps. Add to `canAdvance`:

```javascript
if (screen === "mode") return false;          // the two buttons navigate
if (screen === "invoiceList") return false;   // tapping a row navigates
if (screen === "invoiceDetail") return false; // the Edit button navigates
```

- [ ] **Step 3: Load and render the list**

Add to `app/static/app.js`:

```javascript
async function loadInvoiceList() {
  const list = document.getElementById("invoice-list");
  list.innerHTML = '<div class="secondary">Loading...</div>';
  try {
    const body = await apiGet("/api/" + state.company + "/invoices");
    state.invoices = body.data;
    renderInvoiceList();
  } catch (err) {
    list.innerHTML = "";
    showBanner(err.message || "Could not load invoices", "error");
  }
}

function renderInvoiceList() {
  const list = document.getElementById("invoice-list");
  if (!state.invoices.length) {
    list.innerHTML = '<div class="secondary">No invoices in the last 30 days.</div>';
    return;
  }
  list.innerHTML = state.invoices
    .map(function (inv, index) {
      return (
        '<button type="button" class="list-item" data-invoice-index="' + index + '">' +
        '<div class="primary">' + escapeHtml(inv.doc_no) +
        (inv.is_cancelled ? ' <span class="badge">Cancelled</span>' : "") +
        "</div>" +
        '<div class="secondary">' + escapeHtml(inv.doc_date) + " &middot; " +
        escapeHtml(inv.debtor_code) + " &middot; RM " + escapeHtml(inv.total) +
        " &middot; " + inv.line_count + " line" + (inv.line_count === 1 ? "" : "s") +
        "</div></button>"
      );
    })
    .join("");
}
```

Wire a click handler on `#invoice-list` that reads `data-invoice-index` and calls `openInvoice(state.invoices[index].doc_no)`.

- [ ] **Step 4: Load and render one invoice**

```javascript
async function openInvoice(docNo) {
  try {
    const body = await apiGet("/api/" + state.company + "/invoices/" + encodeURIComponent(docNo));
    state.viewInvoice = body.data;
    current = "invoiceDetail";
    render();
  } catch (err) {
    showBanner(err.message || "Could not open that invoice", "error");
  }
}

function renderInvoiceDetail() {
  const inv = state.viewInvoice;
  if (!inv) return;
  const card = document.getElementById("invoice-detail-card");
  card.innerHTML =
    "<h2>" + escapeHtml(inv.doc_no) + "</h2>" +
    '<div class="secondary">' + escapeHtml(inv.doc_date) + " &middot; " + escapeHtml(inv.debtor_code) + "</div>" +
    (inv.is_editable
      ? '<button type="button" class="btn btn-primary" id="edit-invoice-btn">Edit lines</button>'
      : '<div class="secondary">' +
        (inv.is_cancelled
          ? "This invoice is cancelled and cannot be edited."
          : "This invoice is more than 30 days old. Correct it in AutoCount directly.") +
        "</div>");

  document.getElementById("invoice-detail-lines").innerHTML = inv.lines
    .map(function (line) {
      return (
        '<div class="line-row"><div class="primary">' + escapeHtml(line.product_code) + "</div>" +
        '<div class="secondary">' + escapeHtml(line.description) + "</div>" +
        '<div class="secondary">' + escapeHtml(line.quantity) + " &times; RM " +
        escapeHtml(line.unit_price) + "</div></div>"
      );
    })
    .join("");

  document.getElementById("invoice-detail-total").textContent = "RM " + inv.total;
}
```

- [ ] **Step 5: Verify in the browser**

Start the server, walk company → **Recent invoices**, confirm the list renders newest first, open one, and confirm an old or cancelled invoice shows the explanation instead of the Edit button. Money must render exactly as returned (no rounding).

- [ ] **Step 6: Commit**

```bash
git add app/static/index.html app/static/app.js
git commit -m "feat: browse and view issued invoices on the mobile page

The page could only create invoices; there was no way to see what had been
issued without opening AutoCount. Editability is shown as an explanation
rather than a hidden button so it is clear why an old invoice is read-only."
```

---

## Task 10: Edit screens

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`

**Interfaces:**
- Consumes: `PUT /api/{company}/invoices/{doc_no}` (Task 7); the existing item-search picker.
- Produces: `invoiceEdit` and `invoiceEditConfirm` screens.

- [ ] **Step 1: Add the markup**

```html
<section class="screen" data-screen="invoiceEdit">
  <div class="selected-summary" id="edit-context"></div>
  <div id="edit-line-list"></div>
  <button class="add-item-btn" id="edit-add-item-btn">+ Add item</button>
  <div id="edit-item-picker" style="display:none; margin-top:14px;">
    <input type="search" id="edit-item-search" placeholder="Search item code or name..." autocomplete="off" />
    <div class="list" id="edit-item-search-list"></div>
  </div>
</section>

<section class="screen" data-screen="invoiceEditConfirm">
  <div class="selected-summary" id="edit-confirm-context"></div>
  <div id="edit-diff"></div>
  <div class="review-total">
    <span>New total</span>
    <span id="edit-new-total">RM 0.00</span>
  </div>
</section>
```

- [ ] **Step 2: Seed the edit state from the loaded invoice**

```javascript
function startEdit() {
  const inv = state.viewInvoice;
  state.editDocNo = inv.doc_no;
  // The line set as loaded — sent back as expected_lines so the server can
  // refuse a save built on a stale view.
  state.editOriginal = inv.lines.map(function (line) {
    return {
      item_id: line.product_code,
      quantity: line.quantity,
      unit_price: line.unit_price,
    };
  });
  state.editLines = inv.lines.map(function (line) {
    return {
      item_id: line.product_code,
      description: line.description,
      quantity: line.quantity,
      unit_price: line.unit_price,
    };
  });
  current = "invoiceEdit";
  render();
}
```

- [ ] **Step 3: Render the editable lines**

Render one row per `state.editLines` entry with a quantity input, a unit-price input, and a Remove button. Reuse the create flow's `updateLineTotal` formatting. Removing the last remaining line must be blocked — the server rejects an empty line set with a 422, so disable Remove when `state.editLines.length === 1`.

Wire `#edit-add-item-btn` to the existing item-search picker, pushing the chosen product onto `state.editLines` with its `default_price` prefilled.

- [ ] **Step 4: Render the diff**

```javascript
function renderEditDiff() {
  const before = state.editOriginal;
  const after = state.editLines;
  const rows = [];

  after.forEach(function (line, index) {
    const prior = before[index];
    if (!prior || prior.item_id !== line.item_id) {
      rows.push('<div class="line-row added">+ ' + escapeHtml(line.item_id) + " " +
        escapeHtml(line.quantity) + " &times; RM " + escapeHtml(line.unit_price) + "</div>");
    } else if (prior.quantity !== line.quantity || prior.unit_price !== line.unit_price) {
      rows.push('<div class="line-row changed">~ ' + escapeHtml(line.item_id) + ": " +
        escapeHtml(prior.quantity) + " &times; RM " + escapeHtml(prior.unit_price) + " &rarr; " +
        escapeHtml(line.quantity) + " &times; RM " + escapeHtml(line.unit_price) + "</div>");
    }
  });

  before.slice(after.length).forEach(function (line) {
    rows.push('<div class="line-row removed">&minus; ' + escapeHtml(line.item_id) + "</div>");
  });

  document.getElementById("edit-diff").innerHTML =
    rows.length ? rows.join("") : '<div class="secondary">No changes.</div>';
}
```

- [ ] **Step 5: Save**

```javascript
async function saveEdit() {
  if (state.saving) return;
  state.saving = true;
  try {
    const body = await apiPut(
      "/api/" + state.company + "/invoices/" + encodeURIComponent(state.editDocNo),
      {
        company: state.company,
        expected_lines: state.editOriginal,
        lines: state.editLines.map(function (line) {
          return {
            item_id: line.item_id,
            quantity: line.quantity,
            unit_price: line.unit_price,
          };
        }),
      }
    );
    state.viewInvoice = body.data;
    showBanner("Invoice " + body.data.doc_no + " updated", "success");
    current = "invoiceDetail";
    render();
  } catch (err) {
    // invoice_changed and edit_unconfirmed both mean "reopen it" — the
    // on-screen line set can no longer be trusted, so don't leave the user
    // staring at a stale edit form.
    if (err.code === "invoice_changed" || err.code === "edit_unconfirmed") {
      showBanner(err.message, "error");
      await openInvoice(state.editDocNo);
    } else {
      showBanner(err.message || "Could not save the changes", "error");
    }
  } finally {
    state.saving = false;
  }
}
```

Add the `apiPut` helper next to `apiPost` in `app/static/app.js`, mirroring it exactly but with `method: "PUT"`. Ensure `apiError` carries the response body's `error` code through as `err.code`.

- [ ] **Step 6: Verify in the browser**

Start the server and, against a test invoice: change a quantity and save; add a line and save; remove a line and save. Confirm the diff screen matches what you did each time, and that the detail screen afterwards shows the new state.

- [ ] **Step 7: Commit**

```bash
git add app/static/index.html app/static/app.js
git commit -m "feat: edit an issued invoice's lines from the mobile page

The confirm screen shows the diff before saving because the write replaces
the whole line set. A stale or unconfirmed save reloads the invoice rather
than leaving a form the server has already rejected."
```

---

## Task 11: Live end-to-end verification

Per `agents.md`, a green suite alone does not prove the flow works: AutoCount's documented contract and its live behaviour have diverged before.

**Files:**
- Modify: `docs/testing/manual-test-checklist.md`
- Modify: `README.md`

- [ ] **Step 1: Deploy the branch**

Push and let Vercel build the preview deployment for the branch.

- [ ] **Step 2: Walk the flow against the real account book**

On the preview URL:

1. Issue a fresh invoice through the existing create flow. Note its document number.
2. Company → Recent invoices → confirm the new invoice is at the top.
3. Open it → confirm lines, total, and the Edit button.
4. Edit: change a quantity → confirm the diff → save → confirm the detail screen shows the new quantity.
5. Edit: add a line → save → confirm.
6. Edit: remove a line → save → confirm the line is gone.
7. Open AutoCount's own UI and confirm the invoice matches what the app shows.
8. Confirm an invoice older than 30 days shows as view-only.

- [ ] **Step 3: Verify the GPT Action is unchanged**

```bash
curl -s https://<preview-url>/openapi.json | python -c "import json,sys; print(sorted(json.load(sys.stdin)['paths']))"
```

Expected: the same paths as before this branch — no `invoices/{doc_no}` entries.

- [ ] **Step 4: Record the results**

Add a "View and edit issued invoices" section to `docs/testing/manual-test-checklist.md` covering steps 1–8, and update the README's Status and HTTP API sections: the three new endpoints (noting they are intentionally absent from the GPT schema) and the mobile page's new view/edit flow.

- [ ] **Step 5: Run the full suite one final time**

Run: `python -m pytest tests/ -q`
Expected: 393 passed, 16 skipped

- [ ] **Step 6: Commit**

```bash
git add docs/testing/manual-test-checklist.md README.md
git commit -m "docs: record live verification of the view-and-edit flow"
```

---

## Self-Review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| Browse recent invoices | 3, 4, 9 |
| Open one invoice, see header and lines | 2, 4, 9 |
| Add / remove lines | 5, 6, 10 |
| Edit qty and unit price | 5, 6, 10 |
| Mobile page only, hidden from GPT | 4, 7 (asserted by tests in both) |
| Full-state replace, no `{}` placeholders | 5 |
| Editability boundary, enforced server-side | 4 (`is_editable`), 6 (enforced in `edit`) |
| Stale-write guard | 6 |
| Ambiguous-write reconciliation | 6 |
| No idempotency machinery on edit | 6 (service stores nothing) |
| Error table (404/409×3/502) | 4, 7 |
| `description` on lines | 2 |
| Server-side sort, newest first | 3 |
| `days` bounded 1–30 | 4 |
| Live spike sequenced first | 1 |
| Spike findings written up | 1 |
| Frontend split before new screens | 8 |
| Live E2E verification | 11 |

Out-of-scope items in the spec (header edits, void/cancel, GPT exposure, edit audit rows) have no tasks, as intended.

**Placeholder scan:** No TBD/TODO. Every code step carries real code. Task 10 steps 3 describes rendering in prose rather than full code because it reuses the create flow's existing line-row renderer verbatim; the state shape and the one new constraint (blocking removal of the last line) are both specified.

**Type consistency:** `InvoiceLineSummary(product_code, qty, unit_price, description="")` is defined in Task 2 and used with those names in Tasks 4, 6. `InvoiceEditLine(item_id, quantity, unit_price)` is defined in Task 5 and used in 6, 7, 10. `is_editable(invoice, *, today)` and `EDIT_WINDOW_DAYS` are defined in Task 4 and consumed in 6. `map_invoice_update_payload(lines, products)` is defined in Task 5 and called in 6. `_detail_item(invoice)` is defined in Task 4 and reused by Task 7's `PUT` handler. `InvoiceNotFoundError` is defined in Task 4 and raised by Task 6's `_read`.

**Known ordering constraint:** Task 4 creates `app/services/invoice_edit_service.py` with only `EDIT_WINDOW_DAYS`, `InvoiceEditError`, `InvoiceNotFoundError`, and `is_editable`; Task 6 appends the service class to the same file. Task 4 cannot be skipped or reordered after Task 6.
