"""Map a confirmed mobile invoice draft to AutoCount's invoice input model."""

from collections.abc import Mapping
from typing import Any

from app.models.invoice import InvoiceDraftInput
from app.models.master_data import DeliveryAddress, ProductSummary


def map_invoice_payload(
    draft: InvoiceDraftInput,
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
                "qty": str(line.quantity),
                "unitPrice": str(line.unit_price),
            }
        )

    return {
        "master": {
            "docDate": draft.invoice_date.isoformat(),
            "debtorCode": draft.customer_id,
            "deliverAddress": delivery_address.address_text.strip(),
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
