"""Task 6 — mapping a confirmed mobile draft to AutoCount's invoice API."""

from datetime import date
from decimal import Decimal
import pytest

from app.autocount.mapping import (
    DEFAULT_ACC_NO,
    DEFAULT_CREDIT_TERM,
    DEFAULT_SALES_LOCATION,
    map_invoice_payload,
)
from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput
from app.models.master_data import CustomerSummary, DeliveryAddress, ProductSummary


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


def resolved_customer() -> CustomerSummary:
    return CustomerSummary(id="700-C001", code="700-C001", name="Restoran Contoh")


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
    payload = map_invoice_payload(
        make_draft(), resolved_customer(), resolved_address(), resolved_products()
    )

    assert payload == {
        "master": {
            "docDate": "2026-08-03",
            "debtorCode": "700-C001",
            "debtorName": "Restoran Contoh",
            "address": "1, Jalan Example\n31450 Ipoh",
            "deliverAddress": "1, Jalan Example\n31450 Ipoh",
            "creditTerm": DEFAULT_CREDIT_TERM,
            "salesLocation": DEFAULT_SALES_LOCATION,
            "paymentMethod": "CASH",
            "submitEInvoice": False,
            "submitConsolidatedEInvoice": False,
        },
        "details": [
            {
                "productCode": "OIL-5KG",
                "description": "Cooking Oil 5KG",
                "qty": Decimal("2"),
                "unitPrice": Decimal("31.50"),
                "accNo": DEFAULT_ACC_NO,
            },
            {
                "productCode": "OIL-2KG",
                "description": "Cooking Oil 2KG",
                "qty": Decimal("3.5"),
                "unitPrice": Decimal("13.20"),
                "accNo": DEFAULT_ACC_NO,
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


def test_maps_customer_billing_address_to_invoice_master_address():
    address = DeliveryAddress(
        id="700-C001:delivery",
        label="Default delivery address",
        address_text="Warehouse entrance\n31450 Ipoh",
        billing_address_text="Accounts office\n1, Jalan Billing\n31450 Ipoh",
    )

    payload = map_invoice_payload(
        make_draft(), resolved_customer(), address, resolved_products()
    )

    assert payload["master"]["address"] == (
        "Accounts office\n1, Jalan Billing\n31450 Ipoh"
    )


def test_maps_delivery_address_as_billing_fallback_when_master_billing_is_blank():
    payload = map_invoice_payload(
        make_draft(), resolved_customer(), resolved_address(), resolved_products()
    )

    assert payload["master"]["address"] == "1, Jalan Example\n31450 Ipoh"


def test_maps_cash_tax_entity_and_item_classification_for_quick_invoice():
    customer = CustomerSummary(
        id="700-C001",
        code="700-C001",
        name="Restoran Contoh",
        tax_entity="TIN:C23453889090",
    )
    products = {
        "OIL-5KG": ProductSummary(
            id="OIL-5KG",
            code="OIL-5KG",
            name="Cooking Oil 5KG",
            default_price=Decimal("31.50"),
            classification_code="022",
        ),
        "OIL-2KG": ProductSummary(
            id="OIL-2KG",
            code="OIL-2KG",
            name="Cooking Oil 2KG",
            default_price=Decimal("13.20"),
            classification_code="022",
        ),
    }

    payload = map_invoice_payload(
        make_draft(), customer, resolved_address(), products
    )

    assert payload["master"]["paymentMethod"] == "CASH"
    assert payload["master"]["taxEntity"] == "TIN:C23453889090"
    assert [line["classificationCode"] for line in payload["details"]] == [
        "022",
        "022",
    ]


def test_default_credit_term_and_sales_location_are_wanson_standard_terms():
    """Wanson issues every quick-invoice on the same terms (confirmed with the
    business owner): cash on delivery, out of the single HQ sales location,
    posted to the one trade debtors AR account. These are not derived
    per-customer from AutoCount."""
    assert DEFAULT_CREDIT_TERM == "C.O.D."
    assert DEFAULT_SALES_LOCATION == "HQ"
    assert DEFAULT_ACC_NO == "500-0000"


def test_original_price_and_client_only_fields_never_reach_autocount():
    payload = map_invoice_payload(
        make_draft(), resolved_customer(), resolved_address(), resolved_products()
    )
    serialised = repr(payload)

    assert "original_unit_price" not in serialised
    assert "30.00" not in serialised
    assert "idempotency" not in serialised
    assert "account_book" not in serialised
    assert "docNo" not in payload["master"]


def test_rejects_customer_not_matching_the_confirmed_draft():
    wrong_customer = CustomerSummary(id="700-X999", code="700-X999", name="Someone Else")

    with pytest.raises(ValueError, match="customer"):
        map_invoice_payload(make_draft(), wrong_customer, resolved_address(), resolved_products())


def test_rejects_address_not_selected_in_the_confirmed_draft():
    wrong_address = DeliveryAddress(
        id="700-X999:delivery",
        label="Default delivery address",
        address_text="Wrong customer address",
    )

    with pytest.raises(ValueError, match="delivery address"):
        map_invoice_payload(make_draft(), resolved_customer(), wrong_address, resolved_products())


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
        map_invoice_payload(draft, resolved_customer(), resolved_address(), products)


def test_maps_requested_einvoice_submission_to_autocount_master():
    draft = make_draft().model_copy(update={"submit_einvoice": True})

    payload = map_invoice_payload(
        draft, resolved_customer(), resolved_address(), resolved_products()
    )

    assert payload["master"]["submitEInvoice"] is True
    assert payload["master"]["submitConsolidatedEInvoice"] is False


def test_qty_and_price_preserve_exact_decimal_precision_beyond_float_safety():
    """A price/quantity that would silently round through a binary float must
    reach AutoCount as an exact ``Decimal``, never a float-corrupted value.

    The mapping layer keeps these as ``Decimal`` objects (not strings): the
    transport layer (``app.autocount.client``) is responsible for encoding
    them as exact, unquoted JSON numbers on the wire — see
    ``test_client_encodes_decimal_as_exact_bare_json_number`` in
    ``test_autocount_client.py``. AutoCount rejects a quoted decimal string
    for these fields with a "System.Decimal" conversion error, and float()
    would risk silently rounding an exact price/quantity.
    """
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

    payload = map_invoice_payload(draft, resolved_customer(), resolved_address(), products)
    line = payload["details"][0]

    assert line["qty"] == Decimal("2.123456789012345")
    assert line["unitPrice"] == Decimal("19.995")
    # Guard against a regression to float(), which would silently corrupt
    # precision via IEEE-754 rounding.
    assert isinstance(line["qty"], Decimal)
    assert isinstance(line["unitPrice"], Decimal)
