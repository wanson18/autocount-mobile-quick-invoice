"""Edit-path guards and ambiguous-write resolution.

Two write hazards, both handled without idempotency keys: a stale write (the
invoice changed in AutoCount since it was loaded) and an ambiguous write (the
PUT timed out). Because the request carries absolute desired state, the second
is decidable by re-reading and comparing.
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
    InvoiceEditError,
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


def summary(*, doc_date="2026-08-13T00:00:00", cancelled=False, lines=None):
    return InvoiceSummary(
        id="9001",
        doc_no="CS-034454",
        doc_date=doc_date,
        debtor_code="700-0001",
        total=Decimal("63.00"),
        lines=tuple(
            lines
            if lines is not None
            else [InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil")]
        ),
        is_cancelled=cancelled,
        debtor_name="TANL MARKETING",
        credit_term="C.O.D.",
        sales_location="HQ",
    )


class FakeMasterData:
    """Returns a scripted sequence of reads; the last one repeats."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.read_count = 0
        self.items_requested = []

    async def get_invoice(self, company, invoice_no):
        self.read_count += 1
        value = self.reads[min(self.read_count - 1, len(self.reads) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_item(self, company, item_id):
        self.items_requested.append(item_id)
        return ProductSummary(item_id, item_id, f"Product {item_id}", Decimal("1"))


class ForeignItemMasterData(FakeMasterData):
    async def get_item(self, company, item_id):
        # An item the selected account book does not own comes back under a
        # different code.
        return ProductSummary("OTHER", "OTHER", "Wrong book", Decimal("1"))


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
        else [
            ExpectedLine(
                item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("31.50")
            )
        ],
        lines=lines
        if lines is not None
        else [
            InvoiceEditLine(
                item_id="ITEM-1", quantity=Decimal("5"), unit_price=Decimal("31.50")
            )
        ],
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_edit_writes_the_full_desired_line_set_with_master_echoed():
    after = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("5"), Decimal("31.50"), "Oil")]
    )
    master = FakeMasterData([summary(), after])
    client = FakeClient()

    result = run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))

    method, endpoint, params, body = client.writes[0]
    assert (method, endpoint) == ("PUT", "invoice")
    assert params == {"docNo": "CS-034454"}
    assert body["details"][0]["qty"] == Decimal("5")
    assert body["master"]["debtorName"] == "TANL MARKETING"
    assert body["master"]["docDate"] == "2026-08-13T00:00:00"
    assert len(client.writes) == 1
    assert result.lines[0].qty == Decimal("5")


def test_the_updated_invoice_is_read_back_and_returned():
    after = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("5"), Decimal("31.50"), "Oil")]
    )
    master = FakeMasterData([summary(), after])

    result = run(make_service(master, FakeClient()).edit("CS-034454", edit_input(), today=TODAY))

    # One read before the write for the guards, one after to return the truth.
    assert master.read_count == 2
    assert result is after


def test_removing_a_line_sends_the_shorter_array():
    two_lines = summary(
        lines=[
            InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),
            InvoiceLineSummary("ITEM-2", Decimal("1"), Decimal("42.00"), "Rice"),
        ]
    )
    after = summary()
    master = FakeMasterData([two_lines, after])
    client = FakeClient()

    run(
        make_service(master, client).edit(
            "CS-034454",
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


# ---------------------------------------------------------------------------
# editability
# ---------------------------------------------------------------------------


def test_a_cancelled_invoice_is_not_editable():
    client = FakeClient()
    with pytest.raises(InvoiceNotEditableError):
        run(make_service(FakeMasterData([summary(cancelled=True)]), client).edit(
            "CS-034454", edit_input(), today=TODAY
        ))
    assert client.writes == []


def test_an_invoice_older_than_the_window_is_not_editable():
    client = FakeClient()
    with pytest.raises(InvoiceNotEditableError):
        run(make_service(FakeMasterData([summary(doc_date="2026-06-01")]), client).edit(
            "CS-034454", edit_input(), today=TODAY
        ))
    assert client.writes == []


def test_an_unknown_invoice_is_not_found():
    client = FakeClient()
    with pytest.raises(InvoiceNotFoundError):
        run(make_service(
            FakeMasterData([AutoCountRejectedError(404, "not found")]), client
        ).edit("CS-034454", edit_input(), today=TODAY))
    assert client.writes == []


def test_an_upstream_failure_other_than_404_is_not_swallowed():
    client = FakeClient()
    with pytest.raises(AutoCountRejectedError):
        run(make_service(
            FakeMasterData([AutoCountRejectedError(500, "boom")]), client
        ).edit("CS-034454", edit_input(), today=TODAY))
    assert client.writes == []


# ---------------------------------------------------------------------------
# stale-write guard
# ---------------------------------------------------------------------------


def test_a_changed_quantity_is_rejected_before_writing():
    changed = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("9"), Decimal("31.50"), "Oil")]
    )
    client = FakeClient()
    with pytest.raises(InvoiceChangedError):
        run(make_service(FakeMasterData([changed]), client).edit(
            "CS-034454", edit_input(), today=TODAY
        ))
    assert client.writes == []


def test_a_changed_line_count_is_rejected_before_writing():
    changed = summary(
        lines=[
            InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),
            InvoiceLineSummary("ITEM-2", Decimal("1"), Decimal("42.00"), "Rice"),
        ]
    )
    client = FakeClient()
    with pytest.raises(InvoiceChangedError):
        run(make_service(FakeMasterData([changed]), client).edit(
            "CS-034454", edit_input(), today=TODAY
        ))
    assert client.writes == []


def test_a_reordered_line_set_is_treated_as_changed():
    # Row order is meaningful to AutoCount, so a reorder is a real change.
    two = [
        InvoiceLineSummary("ITEM-2", Decimal("1"), Decimal("42.00"), "Rice"),
        InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),
    ]
    client = FakeClient()
    with pytest.raises(InvoiceChangedError):
        run(make_service(FakeMasterData([summary(lines=two)]), client).edit(
            "CS-034454",
            edit_input(
                expected=[
                    ExpectedLine(item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("31.50")),
                    ExpectedLine(item_id="ITEM-2", quantity=Decimal("1"), unit_price=Decimal("42.00")),
                ]
            ),
            today=TODAY,
        ))
    assert client.writes == []


def test_an_unchanged_line_set_passes_the_guard():
    master = FakeMasterData([summary(), summary()])
    client = FakeClient()
    run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))
    assert len(client.writes) == 1


# ---------------------------------------------------------------------------
# ambiguous write
# ---------------------------------------------------------------------------


def test_an_ambiguous_write_that_applied_is_reported_as_success():
    applied = summary(
        lines=[InvoiceLineSummary("ITEM-1", Decimal("5"), Decimal("31.50"), "Oil")]
    )
    master = FakeMasterData([summary(), applied])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))

    result = run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))
    assert result.lines[0].qty == Decimal("5")


def test_an_ambiguous_write_that_did_not_apply_is_unconfirmed():
    master = FakeMasterData([summary(), summary()])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))

    with pytest.raises(InvoiceEditUnconfirmedError):
        run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))


def test_an_ambiguous_write_is_never_retried():
    master = FakeMasterData([summary(), summary()])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))

    with pytest.raises(InvoiceEditUnconfirmedError):
        run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))
    assert len(client.writes) == 1


def test_an_ambiguous_write_whose_reread_fails_propagates_the_ambiguity():
    # No answer is available, so the caller must see the retryable ambiguity
    # rather than a confident "it did not apply".
    master = FakeMasterData([summary(), AutoCountRejectedError(500, "boom")])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))

    with pytest.raises(AutoCountAmbiguousWriteError):
        run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))


def test_an_ambiguous_write_whose_invoice_vanished_propagates_the_ambiguity():
    master = FakeMasterData([summary(), AutoCountRejectedError(404, "gone")])
    client = FakeClient(AutoCountAmbiguousWriteError("timed out"))

    with pytest.raises(AutoCountAmbiguousWriteError):
        run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))


# ---------------------------------------------------------------------------
# account-book ownership
# ---------------------------------------------------------------------------


def test_an_item_from_another_account_book_is_rejected():
    client = FakeClient()
    master = ForeignItemMasterData([summary(), summary()])

    with pytest.raises(InvoiceEditError):
        run(make_service(master, client).edit("CS-034454", edit_input(), today=TODAY))
    assert client.writes == []


def test_each_distinct_item_is_resolved_once():
    master = FakeMasterData([summary(), summary()])
    run(
        make_service(master, FakeClient()).edit(
            "CS-034454",
            edit_input(
                lines=[
                    InvoiceEditLine(item_id="ITEM-1", quantity=Decimal("1"), unit_price=Decimal("1")),
                    InvoiceEditLine(item_id="ITEM-1", quantity=Decimal("2"), unit_price=Decimal("2")),
                ]
            ),
            today=TODAY,
        )
    )
    assert master.items_requested == ["ITEM-1"]


def test_an_unknown_company_is_rejected():
    service = InvoiceEditService(
        company_resolver=lambda key: None,
        master_data=FakeMasterData([summary()]),
        client=FakeClient(),
    )
    with pytest.raises(InvoiceEditError):
        run(service.edit("CS-034454", edit_input(), today=TODAY))
