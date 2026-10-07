"""Units of measure: read from AutoCount's multipack rows, sent on lines."""

from datetime import date
from decimal import Decimal

import pytest

from app.autocount.adapter import AutoCountMasterDataAdapter
from app.autocount.mapping import map_invoice_payload, map_invoice_update_payload
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput, InvoiceEditLine
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceLineSummary,
    InvoiceSummary,
    ProductSummary,
    ProductUom,
)

PRODUCT = {"productCode": "OIL", "productName": "Oil", "price": 10, "unit": "PCS"}


def _uoms(rows, base="PCS"):
    return AutoCountMasterDataAdapter._product_uoms(rows, base)


def test_multipacks_become_alternate_units():
    uoms = _uoms([{"multiPack": "CTN", "multiPackRate": 12, "price": "110.50"}])
    assert uoms == (ProductUom("CTN", Decimal("12"), Decimal("110.50")),)


def test_unusable_multipacks_are_skipped_not_fatal():
    rows = [
        {"multiPack": "PCS", "multiPackRate": 1, "price": 10},  # the base unit
        {"multiPack": "CTN", "multiPackRate": 12, "price": 1, "productVariant1OptionName": "Red"},
        {"multiPack": "TOOLONGNAME", "multiPackRate": 2, "price": 1},
        {"multiPack": "BAD", "multiPackRate": 0, "price": 1},
        {"multiPack": "NOPRICE", "multiPackRate": 2, "price": "x"},
        {"multiPack": "", "multiPackRate": 2, "price": 1},
        "junk",
    ]
    assert _uoms(rows) == ()
    assert _uoms(None) == ()


def test_product_summary_carries_base_unit_and_uoms():
    summary = AutoCountMasterDataAdapter._product_summary(
        PRODUCT, [{"multiPack": "CTN", "multiPackRate": 12, "price": 110}]
    )
    assert summary.unit == "PCS"
    assert [u.name for u in summary.uoms] == ["CTN"]


def test_invoice_detail_reads_stored_unit():
    line = AutoCountMasterDataAdapter._invoice_detail(
        {"productCode": "OIL", "qty": 2, "unitPrice": 10, "unit": "CTN"}
    )
    assert line.unit == "CTN"


def _draft(unit):
    return InvoiceDraftInput(
        company=CompanyKey.SDN_BHD,
        invoice_date=date(2026, 10, 1),
        customer_id="C1",
        delivery_address_id="C1:delivery",
        lines=[
            {
                "item_id": "OIL",
                "unit": unit,
                "quantity": "1",
                "unit_price": "110",
                "original_unit_price": "110",
            }
        ],
        idempotency_key="k",
    )


def _product():
    return ProductSummary(
        id="OIL", code="OIL", name="Oil", default_price=Decimal("10"), unit="PCS",
        uoms=(ProductUom("CTN", Decimal("12"), Decimal("110")),),
    )


def _create(unit):
    return map_invoice_payload(
        _draft(unit),
        CustomerSummary(id="C1", code="C1", name="Cust"),
        DeliveryAddress(id="C1:delivery", label="x", address_text="addr"),
        {"OIL": _product()},
    )["details"][0]


def test_create_sends_alternate_unit_only():
    assert _create("CTN")["unit"] == "CTN"
    assert "unit" not in _create("PCS")  # base unit: request unchanged
    assert "unit" not in _create(None)


def test_create_rejects_unknown_unit():
    with pytest.raises(ValueError, match="not available"):
        _create("BOX")


def _invoice(unit):
    return InvoiceSummary(
        id="1", doc_no="I-1", doc_date="2026-10-01", debtor_code="C1", total=Decimal("1"),
        lines=(InvoiceLineSummary("OIL", Decimal("1"), Decimal("1"), unit=unit),),
        debtor_name="Cust", credit_term="C.O.D.", sales_location="HQ",
    )


def _update(stored_unit, line_unit):
    line = InvoiceEditLine(item_id="OIL", unit=line_unit, quantity="1", unit_price="1")
    return map_invoice_update_payload(_invoice(stored_unit), [line], {"OIL": _product()})["details"][0]


def test_update_echoes_the_unit_on_every_row():
    assert _update("CTN", "CTN")["unit"] == "CTN"
    assert _update("PCS", "PCS")["unit"] == "PCS"  # stored on the invoice: echoed
    assert "unit" not in _update("", None)


def test_update_keeps_a_stored_unit_the_product_no_longer_offers():
    assert _update("OLD", "OLD")["unit"] == "OLD"
    with pytest.raises(ValueError):
        _update("", "OLD")


def test_listing_rows_never_supply_units():
    """A listing row's multipacks are not that product's own (31 mixed rows
    seen live for an item whose single-product read has 2)."""
    row = {
        "product": PRODUCT,
        "productMultiPacks": [{"multiPack": "BOX", "multiPackRate": 24, "price": 1}],
    }
    summary = AutoCountMasterDataAdapter._product_row(row)
    assert summary.unit == "PCS"
    assert summary.uoms == ()


def test_live_shape_for_a_multi_uom_item():
    """Shape measured live for item 00010: base PKT, multipacks BOX and BOX12."""
    summary = AutoCountMasterDataAdapter._product_summary(
        {**PRODUCT, "unit": "PKT"},
        [
            {"productVariantId": None, "productVariant1OptionName": None,
             "productVariant2OptionName": None, "multiPack": "BOX",
             "multiPackRate": 10.0, "price": 55.0, "minPrice": 50.0,
             "barCode": None, "unitType": None},
            {"multiPack": "BOX12", "multiPackRate": 12.0, "price": 66.0},
        ],
    )
    assert summary.unit == "PKT"
    assert [(u.name, u.rate) for u in summary.uoms] == [
        ("BOX", Decimal("10.0")),
        ("BOX12", Decimal("12.0")),
    ]
