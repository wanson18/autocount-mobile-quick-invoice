"""Task 6 — mapping a confirmed mobile draft to AutoCount's invoice API."""

from datetime import date
from decimal import Decimal

import pytest

from app.autocount.mapping import map_invoice_payload
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import DeliveryAddress, ProductSummary


def make_draft() -> InvoiceDraftInput:
    return InvoiceDraftInput(
        company=CompanyKey.SDN_BHD,
        invoice_date=date(2026, 8, 3),
        customer_id="700-C001",
        delivery_address_id="700-C001:delivery",
        lines=[
            {
                "item_id": "OIL-5KG",
                "quantity": Decimal("2"),
                "unit_price": Decimal("31.50"),
                "original_unit_price": Decimal("30.00"),
            },
            {
                "item_id": "OIL-2KG",
                "quantity": Decimal("3.5"),
                "unit_price": Decimal("13.20"),
                "original_unit_price": Decimal("13.20"),
            },
        ],
        submit_einvoice=False,
        idempotency_key="request-123",
    )


def resolved_address() -> DeliveryAddress:
    return DeliveryAddress(
        id="700-C001:delivery",
        label="Default delivery address",
        address_text="1, Jalan Example\n31450 Ipoh",
    )


def resolved_products() -> dict[str, ProductSummary]:
    return {
        "OIL-5KG": ProductSummary(
            id="OIL-5KG",
            code="OIL-5KG",
            name="Cooking Oil 5KG",
            default_price=Decimal("30.00"),
        ),
        "OIL-2KG": ProductSummary(
            id="OIL-2KG",
            code="OIL-2KG",
            name="Cooking Oil 2KG",
            default_price=Decimal("13.20"),
        ),
    }


def test_maps_confirmed_draft_to_approved_invoice_payload():
    payload = map_invoice_payload(make_draft(), resolved_address(), resolved_products())

    assert payload == {
        "master": {
            "docDate": "2026-08-03",
            "debtorCode": "700-C001",
            "deliverAddress": "1, Jalan Example\n31450 Ipoh",
            "submitEInvoice": False,
            "submitConsolidatedEInvoice": False,
        },
        "details": [
            {
                "productCode": "OIL-5KG",
                "description": "Cooking Oil 5KG",
                "qty": "2",
                "unitPrice": "31.50",
            },
            {
                "productCode": "OIL-2KG",
                "description": "Cooking Oil 2KG",
                "qty": "3.5",
                "unitPrice": "13.20",
            },
        ],
        "autoFillOption": {
            "accNo": True,
            "taxCode": True,
            "tariffCode": True,
            "localTotalCost": True,
        },
        "saveApprove": True,
    }


def test_original_price_and_client_only_fields_never_reach_autocount():
    payload = map_invoice_payload(make_draft(), resolved_address(), resolved_products())
    serialised = repr(payload)

    assert "original_unit_price" not in serialised
    assert "30.00" not in serialised
    assert "idempotency" not in serialised
    assert "account_book" not in serialised
    assert "docNo" not in payload["master"]


def test_rejects_address_not_selected_in_the_confirmed_draft():
    wrong_address = DeliveryAddress(
        id="700-X999:delivery",
        label="Default delivery address",
        address_text="Wrong customer address",
    )

    with pytest.raises(ValueError, match="delivery address"):
        map_invoice_payload(make_draft(), wrong_address, resolved_products())


def test_rejects_missing_or_mismatched_resolved_product():
    draft = make_draft()
    products = resolved_products()
    products["OIL-5KG"] = ProductSummary(
        id="OTHER",
        code="OTHER",
        name="Wrong product",
        default_price=Decimal("30.00"),
    )

    with pytest.raises(ValueError, match="OIL-5KG"):
        map_invoice_payload(draft, resolved_address(), products)


def test_submit_einvoice_cannot_be_smuggled_into_this_approval_flow():
    draft = make_draft().model_copy(update={"submit_einvoice": True})

    with pytest.raises(ValueError, match="e-Invoice"):
        map_invoice_payload(draft, resolved_address(), resolved_products())


def test_qty_and_price_preserve_exact_decimal_precision_beyond_float_safety():
    """A price/quantity that would silently round through a binary float must
    reach AutoCount byte-for-byte as the original decimal text, never as a
    JSON number produced by float()."""
    from app.models.invoice import InvoiceLineInput

    draft = make_draft().model_copy(
        update={
            "lines": [
                InvoiceLineInput(
                    item_id="OIL-5KG",
                    quantity=Decimal("2.123456789012345"),
                    unit_price=Decimal("19.995"),
                    original_unit_price=Decimal("19.995"),
                )
            ]
        }
    )
    products = {
        "OIL-5KG": ProductSummary(
            id="OIL-5KG",
            code="OIL-5KG",
            name="Cooking Oil 5KG",
            default_price=Decimal("19.995"),
        )
    }

    payload = map_invoice_payload(draft, resolved_address(), products)
    line = payload["details"][0]

    assert line["qty"] == "2.123456789012345"
    assert line["unitPrice"] == "19.995"
    # float() would corrupt this value (e.g. via IEEE-754 rounding); guard
    # against a regression back to float() by asserting the JSON type too.
    assert isinstance(line["qty"], str)
    assert isinstance(line["unitPrice"], str)
