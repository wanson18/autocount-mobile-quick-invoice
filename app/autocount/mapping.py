"""Map a confirmed mobile invoice draft to AutoCount's invoice input model."""

from collections.abc import Mapping
from typing import Any

from app.models.invoice import InvoiceDraftInput
from app.models.master_data import CustomerSummary, DeliveryAddress, ProductSummary

#: Wanson issues every quick-invoice on the same standard terms: cash on
#: delivery, out of the single HQ sales location. Confirmed with the business
#: owner (not derived from AutoCount) because these aren't per-customer here.
DEFAULT_CREDIT_TERM = "COD"
DEFAULT_SALES_LOCATION = "HQ"


def map_invoice_payload(
    draft: InvoiceDraftInput,
    customer: CustomerSummary,
    delivery_address: DeliveryAddress,
    products: Mapping[str, ProductSummary],
) -> dict[str, Any]:
    """Build the approved, non-e-Invoice AutoCount payload.

    Customer, address, and products must already have been resolved from the
    selected server-side account book. The confirmed ``unit_price`` is sent to
    AutoCount; ``original_unit_price`` remains audit metadata outside the
    accounting payload.
    """
    if draft.submit_einvoice:
        raise ValueError("e-Invoice submission is a separate workflow")
    if customer.id != draft.customer_id or customer.code != draft.customer_id:
        raise ValueError("resolved customer does not match the confirmed draft")
    if delivery_address.id != draft.delivery_address_id:
        raise ValueError("resolved delivery address does not match the confirmed draft")
    if not delivery_address.address_text.strip():
        raise ValueError("resolved delivery address must not be blank")

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
            }
        )

    return {
        "master": {
            "docDate": draft.invoice_date.isoformat(),
            "debtorCode": draft.customer_id,
            "debtorName": customer.name,
            "deliverAddress": delivery_address.address_text.strip(),
            "creditTerm": DEFAULT_CREDIT_TERM,
            "salesLocation": DEFAULT_SALES_LOCATION,
            "submitEInvoice": False,
            "submitConsolidatedEInvoice": False,
        },
        "details": details,
        "autoFillOption": {
            "accNo": True,
            "taxCode": True,
            "tariffCode": True,
            "localTotalCost": True,
        },
        "saveApprove": True,
    }
