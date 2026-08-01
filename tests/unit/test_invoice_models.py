"""Task 2 — Strict domain models for mobile invoice entry.

Validation outcomes are asserted through public behaviour only: successful
construction and value types, or structured Pydantic ValidationError data
(``errors()``). Assertions rely on stable error ``type`` and ``loc`` values
rather than human-readable message text, which is not version-stable across
``pydantic>=2``.
"""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.company import CompanyKey
from app.models.invoice import InvoiceDraftInput, InvoiceLineInput


def make_line(**overrides):
    values = {
        "item_id": "item-1",
        "quantity": Decimal("2"),
        "unit_price": Decimal("12.50"),
        "original_unit_price": Decimal("15.00"),
    }
    values.update(overrides)
    return values


def make_draft(**overrides):
    values = {
        "company": CompanyKey.ENTERPRISE,
        "invoice_date": date(2026, 8, 1),
        "customer_id": "cust-1",
        "delivery_address_id": "addr-1",
        "lines": [make_line()],
        "submit_einvoice": False,
        "idempotency_key": "key-1",
    }
    values.update(overrides)
    return values


def capture_error(call, loc):
    """Return the first ValidationError entry located at ``loc``."""
    with pytest.raises(ValidationError) as exc:
        call()
    errors = exc.value.errors()
    for err in errors:
        if err["loc"] == loc:
            return err
    raise AssertionError(f"no validation error at {loc}; got {errors}")


def test_valid_line_creates_decimal_values():
    line = InvoiceLineInput(**make_line())
    assert isinstance(line.quantity, Decimal)
    assert isinstance(line.unit_price, Decimal)
    assert isinstance(line.original_unit_price, Decimal)
    assert line.quantity == Decimal("2")
    assert line.unit_price == Decimal("12.50")
    assert line.original_unit_price == Decimal("15.00")


def test_valid_line_accepts_string_decimals():
    line = InvoiceLineInput(**make_line(quantity="2", unit_price="12.50", original_unit_price="15.00"))
    assert isinstance(line.quantity, Decimal)
    assert line.quantity == Decimal("2")


def test_valid_draft_creates_nested_line_models():
    draft = InvoiceDraftInput(**make_draft())
    assert draft.company is CompanyKey.ENTERPRISE
    assert draft.invoice_date == date(2026, 8, 1)
    assert len(draft.lines) == 1
    assert isinstance(draft.lines[0], InvoiceLineInput)
    assert draft.lines[0].item_id == "item-1"


def test_submit_einvoice_defaults_to_false():
    values = {k: v for k, v in make_draft().items() if k != "submit_einvoice"}
    draft = InvoiceDraftInput(**values)
    assert draft.submit_einvoice is False


def test_submit_einvoice_accepts_explicit_true():
    draft = InvoiceDraftInput(**make_draft(submit_einvoice=True))
    assert draft.submit_einvoice is True


def test_submit_einvoice_accepts_explicit_false():
    draft = InvoiceDraftInput(**make_draft(submit_einvoice=False))
    assert draft.submit_einvoice is False


@pytest.mark.parametrize("value", ["yes", "false", "true", "1", "0"])
def test_string_boolean_lookalikes_rejected(value):
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(submit_einvoice=value)),
        ("submit_einvoice",),
    )
    assert err["type"] == "bool_type"


@pytest.mark.parametrize("value", [1, 0])
def test_integer_boolean_lookalikes_rejected(value):
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(submit_einvoice=value)),
        ("submit_einvoice",),
    )
    assert err["type"] == "bool_type"


def test_zero_quantity_rejected():
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(quantity=Decimal("0"))),
        ("quantity",),
    )
    assert err["type"] == "greater_than"


def test_negative_quantity_rejected():
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(quantity=Decimal("-1"))),
        ("quantity",),
    )
    assert err["type"] == "greater_than"


def test_negative_unit_price_rejected():
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(unit_price=Decimal("-0.01"))),
        ("unit_price",),
    )
    assert err["type"] == "greater_than_equal"


def test_negative_original_unit_price_rejected():
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(original_unit_price=Decimal("-0.01"))),
        ("original_unit_price",),
    )
    assert err["type"] == "greater_than_equal"


def test_zero_prices_accepted():
    line = InvoiceLineInput(
        **make_line(unit_price=Decimal("0"), original_unit_price=Decimal("0"))
    )
    assert line.unit_price == Decimal("0")
    assert line.original_unit_price == Decimal("0")


@pytest.mark.parametrize(
    "field, value",
    [
        ("quantity", Decimal("NaN")),
        ("quantity", Decimal("Infinity")),
        ("quantity", Decimal("-Infinity")),
        ("unit_price", Decimal("NaN")),
        ("unit_price", Decimal("Infinity")),
        ("unit_price", Decimal("-Infinity")),
        ("original_unit_price", Decimal("NaN")),
        ("original_unit_price", Decimal("Infinity")),
        ("original_unit_price", Decimal("-Infinity")),
    ],
)
def test_non_finite_decimal_rejected(field, value):
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(**{field: value})),
        (field,),
    )
    assert err["type"] == "finite_number"


def test_empty_lines_rejected():
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(lines=[])),
        ("lines",),
    )
    assert err["type"] == "too_short"


def test_invalid_company_rejected():
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(company="not-a-company")),
        ("company",),
    )
    assert err["type"] == "enum"


@pytest.mark.parametrize(
    "field",
    ["company", "invoice_date", "customer_id", "delivery_address_id", "idempotency_key"],
)
def test_missing_draft_required_field_rejected(field):
    values = {k: v for k, v in make_draft().items() if k != field}
    err = capture_error(lambda: InvoiceDraftInput(**values), (field,))
    assert err["type"] == "missing"


@pytest.mark.parametrize(
    "field",
    ["item_id", "quantity", "unit_price", "original_unit_price"],
)
def test_missing_line_required_field_rejected(field):
    values = {k: v for k, v in make_line().items() if k != field}
    err = capture_error(lambda: InvoiceLineInput(**values), (field,))
    assert err["type"] == "missing"


@pytest.mark.parametrize(
    "field",
    ["item_id", "customer_id", "delivery_address_id", "idempotency_key"],
)
def test_whitespace_only_identifier_rejected(field):
    values = make_line() if field == "item_id" else make_draft()
    values = dict(values)
    values[field] = "   "
    model = InvoiceLineInput if field == "item_id" else InvoiceDraftInput
    err = capture_error(lambda: model(**values), (field,))
    assert err["type"] == "string_too_short"


def test_identifiers_have_surrounding_whitespace_stripped():
    line = InvoiceLineInput(**make_line(item_id="  item-1  "))
    draft = InvoiceDraftInput(
        **make_draft(customer_id="  cust-1  ", delivery_address_id="  addr-1  ", idempotency_key="  key-1  ")
    )
    assert line.item_id == "item-1"
    assert draft.customer_id == "cust-1"
    assert draft.delivery_address_id == "addr-1"
    assert draft.idempotency_key == "key-1"


def test_unexpected_draft_field_rejected():
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(surprise="boom")),
        ("surprise",),
    )
    assert err["type"] == "extra_forbidden"


def test_unexpected_nested_line_field_rejected():
    err = capture_error(
        lambda: InvoiceLineInput(**make_line(surprise="boom")),
        ("surprise",),
    )
    assert err["type"] == "extra_forbidden"


def test_client_supplied_account_book_id_rejected():
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(account_book_id="ab-wanson-enterprise-001")),
        ("account_book_id",),
    )
    assert err["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "field",
    [
        "remarks",
        "invoice_number",
        "po_reference",
        "salesperson",
        "total",
        "tax",
        "einvoice_status",
        "pdf_url",
    ],
)
def test_prohibited_fields_rejected_through_extra_rule(field):
    err = capture_error(
        lambda: InvoiceDraftInput(**make_draft(**{field: "anything"})),
        (field,),
    )
    assert err["type"] == "extra_forbidden"


def test_draft_rejects_invalid_nested_line():
    values = make_draft()
    values["lines"] = [make_line(quantity=Decimal("0"))]
    err = capture_error(
        lambda: InvoiceDraftInput(**values),
        ("lines", 0, "quantity"),
    )
    assert err["type"] == "greater_than"
