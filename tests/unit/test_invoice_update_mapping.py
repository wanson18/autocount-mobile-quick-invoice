"""Update payload contract for AutoCount's positional details array.

Every edit sends the complete desired line set: AutoCount rewrites detail rows
by position and deletes any row beyond the end of the array, so absolute
desired state is the only encoding safe to send twice.

``master`` is required and is echoed from the invoice being edited -- omitting
it is rejected with ``400 The Master field is required``. Optional header
fields are deliberately not sent; they survive untouched. Both confirmed live,
see ``docs/autocount/invoice-update-spike.md``.
"""

import json
from decimal import Decimal

import pytest

from app.autocount.client import _encode_json_body
from app.autocount.mapping import DEFAULT_ACC_NO, map_invoice_update_payload
from app.models.invoice import InvoiceEditLine
from app.models.master_data import (
    InvoiceLineSummary,
    InvoiceSummary,
    ProductSummary,
)

OIL = ProductSummary("ITEM-1", "ITEM-1", "Cooking Oil 5kg", Decimal("31.50"))
RICE = ProductSummary("ITEM-2", "ITEM-2", "Rice 10kg", Decimal("42.00"))
PRODUCTS = {"ITEM-1": OIL, "ITEM-2": RICE}


def invoice(
    *,
    doc_date="2026-08-13T00:00:00",
    debtor_code="700-0001",
    debtor_name="TANL MARKETING",
    credit_term="C.O.D.",
    sales_location="HQ",
):
    return InvoiceSummary(
        id="9001",
        doc_no="CS-034454",
        doc_date=doc_date,
        debtor_code=debtor_code,
        total=Decimal("63.00"),
        lines=(InvoiceLineSummary("ITEM-1", Decimal("2"), Decimal("31.50"), "Oil"),),
        is_cancelled=False,
        debtor_name=debtor_name,
        credit_term=credit_term,
        sales_location=sales_location,
    )


def line(item_id, qty, price):
    return InvoiceEditLine(
        item_id=item_id, quantity=Decimal(qty), unit_price=Decimal(price)
    )


# ---------------------------------------------------------------------------
# master
# ---------------------------------------------------------------------------


def test_master_carries_exactly_the_mandatory_fields():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "2", "31.50")], PRODUCTS
    )

    assert payload["master"] == {
        "docDate": "2026-08-13T00:00:00",
        "debtorCode": "700-0001",
        "debtorName": "TANL MARKETING",
        "creditTerm": "C.O.D.",
        "salesLocation": "HQ",
    }


def test_optional_header_fields_are_not_sent():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "2", "31.50")], PRODUCTS
    )
    # Unsent optional fields are preserved by AutoCount; sending a blank would
    # erase them, which is the whole reason they are omitted.
    for field in ("deliverAddress", "description", "remark1", "docNo", "ref"):
        assert field not in payload["master"]


def test_doc_date_is_echoed_verbatim_not_reformatted():
    payload = map_invoice_update_payload(
        invoice(doc_date="2026-08-13T00:00:00"),
        [line("ITEM-1", "1", "1.00")],
        PRODUCTS,
    )
    assert payload["master"]["docDate"] == "2026-08-13T00:00:00"


def test_the_payload_has_only_master_and_details():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "1", "1.00")], PRODUCTS
    )
    assert set(payload) == {"master", "details"}
    # The invoice is already approved; re-approving is not this call's job.
    assert "saveApprove" not in payload


@pytest.mark.parametrize(
    "field", ["debtor_name", "credit_term", "sales_location", "debtor_code", "doc_date"]
)
def test_a_blank_mandatory_master_field_is_rejected(field):
    # AutoCount would reject the update anyway; failing here keeps the reason
    # legible instead of surfacing an opaque upstream 400.
    with pytest.raises(ValueError, match="mandatory"):
        map_invoice_update_payload(
            invoice(**{field: "  "}), [line("ITEM-1", "1", "1.00")], PRODUCTS
        )


# ---------------------------------------------------------------------------
# details
# ---------------------------------------------------------------------------


def test_every_row_is_fully_specified_in_order():
    payload = map_invoice_update_payload(
        invoice(),
        [line("ITEM-2", "1", "42.00"), line("ITEM-1", "3", "30.00")],
        PRODUCTS,
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
        invoice(), [line("ITEM-1", "1", "1.00"), line("ITEM-2", "1", "2.00")], PRODUCTS
    )
    assert all(row != {} for row in payload["details"])
    assert all(
        {"productCode", "qty", "unitPrice", "accNo"} <= set(row)
        for row in payload["details"]
    )


def test_removing_a_line_simply_sends_fewer_rows():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "1", "1.00")], PRODUCTS
    )
    assert len(payload["details"]) == 1


def test_quantities_and_prices_stay_decimal_never_float():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "2.5", "31.55")], PRODUCTS
    )
    row = payload["details"][0]

    assert isinstance(row["qty"], Decimal)
    assert isinstance(row["unitPrice"], Decimal)
    assert not isinstance(row["qty"], float)


def test_encoded_body_sends_bare_json_numbers():
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "2.5", "31.55")], PRODUCTS
    )
    text = _encode_json_body(payload).decode()

    assert '"qty":2.5' in text
    assert '"unitPrice":31.55' in text
    assert '"qty":"' not in text
    assert json.loads(text)["details"][0]["unitPrice"] == 31.55


def test_a_zero_unit_price_is_allowed():
    # Observed live: product 00004 carries a 0.00 master price.
    payload = map_invoice_update_payload(
        invoice(), [line("ITEM-1", "1", "0")], PRODUCTS
    )
    assert payload["details"][0]["unitPrice"] == Decimal("0")


def test_an_unresolved_product_is_rejected():
    with pytest.raises(ValueError, match="ITEM-9"):
        map_invoice_update_payload(invoice(), [line("ITEM-9", "1", "1.00")], PRODUCTS)


def test_a_mismatched_product_identity_is_rejected():
    wrong = {"ITEM-1": ProductSummary("ITEM-1", "OTHER", "Wrong", Decimal("1"))}
    with pytest.raises(ValueError):
        map_invoice_update_payload(invoice(), [line("ITEM-1", "1", "1.00")], wrong)


def test_an_empty_line_set_is_rejected():
    with pytest.raises(ValueError, match="at least one line"):
        map_invoice_update_payload(invoice(), [], PRODUCTS)
