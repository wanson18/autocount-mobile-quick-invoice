"""Map a confirmed mobile invoice draft to AutoCount's invoice input model."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.models.invoice import InvoiceDraftInput, InvoiceEditLine
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceSummary,
    ProductSummary,
)

#: Wanson issues every quick-invoice on the same standard terms: cash on
#: delivery, out of the single HQ sales location. Confirmed with the business
#: owner (not derived from AutoCount) because these aren't per-customer here.
#: The credit term must match the exact CreditTermKey configured in AutoCount
#: (Master Data > Credit Term). Confirmed from a screenshot of the actual
#: AutoCount dropdown after "COD" and "C.O.D" were both rejected live with
#: "CreditTerm (CreditTermKey = ...) not exists"; the real key has a trailing
#: period: "C.O.D.".
DEFAULT_CREDIT_TERM = "C.O.D."
DEFAULT_SALES_LOCATION = "HQ"
# AutoCount's configured cash payment method. This selects Cash as the
# payment method; it does not imply that a payment amount has been received.
DEFAULT_PAYMENT_METHOD = "CASH"

#: AutoCount's Invoice Detail Input Model requires ``accNo`` on every line —
#: it is the Sales GL account (Chart of Accounts code), NOT a customer/debtor
#: field, and NOT part of the invoice master (the master model has no accNo
#: field at all; see
#: https://accounting-api.autocountcloud.com/documentation/models/invoice/inputmodels/invoice-detail-inputmodel/).
#: autoFillOption.accNo=true is supposed to auto-resolve this per product via
#: AutoCount's "Product Posting" setup, but that isn't configured for these
#: products, so AutoCount still rejects the invoice with "AccNo is required."
#: Sending it explicitly on every line with Wanson's one sales account
#: (confirmed with the business owner) works regardless of Product Posting
#: config.
DEFAULT_ACC_NO = "500-0000"


def map_invoice_payload(
    draft: InvoiceDraftInput,
    customer: CustomerSummary,
    delivery_address: DeliveryAddress,
    products: Mapping[str, ProductSummary],
) -> dict[str, Any]:
    """Build the approved AutoCount payload, including the e-Invoice request.

    Customer, address, and products must already have been resolved from the
    selected server-side account book. The confirmed ``unit_price`` is sent to
    AutoCount; ``original_unit_price`` remains audit metadata outside the
    accounting payload.
    """
    if customer.id != draft.customer_id or customer.code != draft.customer_id:
        raise ValueError("resolved customer does not match the confirmed draft")
    if delivery_address.id != draft.delivery_address_id:
        raise ValueError("resolved delivery address does not match the confirmed draft")
    delivery_address_text = delivery_address.address_text.strip()
    if not delivery_address_text:
        raise ValueError("resolved delivery address must not be blank")
    billing_address_text = delivery_address.billing_address_text.strip()
    if not billing_address_text:
        # AutoCount keeps billing and delivery addresses in separate invoice
        # master fields. Older/customer records may have no billing address;
        # the confirmed customer-owned delivery address is the least
        # surprising printable fallback and preserves the existing flow.
        billing_address_text = delivery_address_text

    details: list[dict[str, Any]] = []
    for line in draft.lines:
        product = products.get(line.item_id)
        if product is None or product.id != line.item_id or product.code != line.item_id:
            raise ValueError(f"resolved product does not match item {line.item_id}")
        details.append(
            {
                "productCode": product.code,
                "description": product.name,
                # Decimal, not str(): the transport layer (app.autocount.client)
                # encodes Decimal as an exact, unquoted JSON number. AutoCount's
                # API rejects a quoted decimal string for these numeric fields
                # with a "System.Decimal" conversion error, and float() would
                # risk silently rounding an exact price/quantity.
                "qty": line.quantity,
                "unitPrice": line.unit_price,
                # Required per-line Sales GL account; see DEFAULT_ACC_NO above.
                "accNo": DEFAULT_ACC_NO,
                # Product classification is sourced from the authoritative
                # product lookup; never infer it from the item description.
                **(
                    {"classificationCode": product.classification_code}
                    if getattr(product, "classification_code", None)
                    else {}
                ),
            }
        )

    master = {
        "docDate": draft.invoice_date.isoformat(),
        "debtorCode": draft.customer_id,
        "debtorName": customer.name,
        "address": billing_address_text,
        "deliverAddress": delivery_address_text,
        "creditTerm": DEFAULT_CREDIT_TERM,
        "salesLocation": DEFAULT_SALES_LOCATION,
        "paymentMethod": DEFAULT_PAYMENT_METHOD,
        # AutoCount starts e-Invoice processing from this documented master
        # flags. The mobile preview currently requests both individual and
        # consolidated e-Invoice processing for every invoice.
        "submitEInvoice": draft.submit_einvoice,
        "submitConsolidatedEInvoice": True,
    }
    if getattr(customer, "tax_entity", None):
        # The debtor's linked tax entity is sourced from the authoritative
        # customer lookup; never substitute the seller's tax entity here.
        master["taxEntity"] = customer.tax_entity

    return {
        "master": master,
        "details": details,
        "autoFillOption": {
            "accNo": True,
            "taxCode": True,
            "tariffCode": True,
            "localTotalCost": True,
        },
        "saveApprove": True,
    }


#: The invoice master's mandatory fields, mapped from ``InvoiceSummary`` to
#: AutoCount's field names. Update Invoice requires ``master``: a body without
#: it is rejected with "The Master field is required." (confirmed live, see
#: docs/autocount/invoice-update-spike.md), so an edit echoes these five back
#: from the invoice being changed.
_MASTER_ECHO_FIELDS = (
    ("docDate", "doc_date"),
    ("debtorCode", "debtor_code"),
    ("debtorName", "debtor_name"),
    ("creditTerm", "credit_term"),
    ("salesLocation", "sales_location"),
)


def map_invoice_update_payload(
    invoice: InvoiceSummary,
    lines: Sequence[InvoiceEditLine],
    products: Mapping[str, ProductSummary],
) -> dict[str, Any]:
    """Build the AutoCount Update Invoice body for a complete desired line set.

    AutoCount's Update Invoice treats ``details`` as a positional array: row N
    of the request overwrites row N of the stored invoice, and any stored row
    past the end of the array is deleted
    (https://accounting-api.autocountcloud.com/documentation/api-methods/invoice/update-invoice/).
    The documented way to leave a row alone is an empty ``{}`` in its slot,
    but this builder never emits one: it spells out every surviving row in
    full, so removing the middle line of three is just sending rows one and
    three. That makes the request absolute desired state, which is what lets a
    timed-out write be resolved by re-reading instead of guessed at.

    ``master`` carries only the five mandatory fields, echoed verbatim from
    the invoice being edited -- ``docDate`` included, which AutoCount returns
    as a datetime and which must not be reformatted. Every optional header
    field is left unsent and is preserved rather than blanked; that is how a
    line edit leaves the header alone. A blank mandatory field is rejected
    here so the reason stays legible instead of arriving as an opaque
    upstream 400.

    ``description`` comes from the resolved product master, exactly as
    ``map_invoice_payload`` does on create, so an edited invoice carries the
    same descriptions a freshly created one would.
    """
    if not lines:
        raise ValueError("an invoice must keep at least one line")

    master: dict[str, Any] = {}
    for field, attribute in _MASTER_ECHO_FIELDS:
        value = getattr(invoice, attribute, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"invoice {invoice.doc_no} is missing the mandatory master "
                f"field {field!r}; it cannot be echoed back on an update"
            )
        master[field] = value

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
    return {"master": master, "details": details}
