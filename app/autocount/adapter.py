"""Read-only master-data adapter over the AutoCount Cloud Accounting API.

Every call goes through ``AutoCountClient.read``, so the server-side
``CompanyConfig`` selects the account book; results are never cached, merged,
or reused across companies. The AutoCount endpoints provide no documented
server-side free-text search, so listing pages are consumed locally and
filtered with a trimmed, case-insensitive substring match against code or name.

Normalisation and isolation:

- A customer's identity is its AutoCount code (``id == code``).
- A product's identity is its AutoCount lookup code (``id == code``) and its
  ``default_price`` is the exact ``Decimal`` of the documented master
  ``price`` (numeric string or number; never a binary float).
- A delivery address is owned by its customer: the stable adapter ID is
  ``"<customer_id>:delivery"`` and a mismatched debtor is rejected.
- Customer detail retains the debtor's linked ``taxEntity`` and product detail
  retains ``classificationCode`` so the issue payload can carry both values.
- An invoice's identity is its AutoCount document key (``docKey``); invoice
  listing rows carry the documented ``master``/``details`` view-model shape.
- Malformed or inconsistent 2xx payloads fail closed with
  ``AutoCountDataError``, whose messages never include the raw body.
  ``AutoCountRejectedError`` and transport errors propagate unchanged.

No creation, variant selection, pricing rules, retries, invoice mapping,
routes, or UI work lives here.
"""

from __future__ import annotations

import decimal
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from app.autocount.client import AutoCountClient
from app.autocount.errors import AutoCountDataError, AutoCountUnsupportedError
from app.config import CompanyConfig
from app.models.master_data import (
    CustomerSummary,
    DeliveryAddress,
    InvoiceLineSummary,
    InvoiceSummary,
    PriceHistory,
    ProductSummary,
)

DEFAULT_ADDRESS_LABEL = "Default delivery address"
_LISTING_PAGE_LIMIT = 1000
_ACTIVE_STATUSES = {
    "active": True,
    "inactive": False,
    "discontinued": False,
}
#: How far back price history searches for prior invoices. A bounded window
#: keeps the listing paging bounded; 90 days covers the customer's recent
#: pricing for the confirmation preview.
PRICE_HISTORY_WINDOW_DAYS = 90


class AutoCountMasterDataAdapter:
    """Fetches and normalises AutoCount master data for one account book per call."""

    def __init__(self, client: AutoCountClient) -> None:
        self._client = client

    async def search_customers(
        self, company: CompanyConfig, query: str
    ) -> list[CustomerSummary]:
        """All active customers whose code or name contains ``query`` (case-insensitive)."""
        self._validate_query(query)
        summaries = await self._listing(
            company,
            "GET",
            "debtor/listing",
            params_for=lambda page: {"page": page, "activeOnly": "true"},
            body_for=None,
            extract=self._customer_row,
        )
        return self._filter(summaries, query)

    async def get_delivery_addresses(
        self, company: CompanyConfig, customer_id: str
    ) -> list[DeliveryAddress]:
        """The selected customer's default delivery address, or ``[]`` when blank.

        The returned ``accNo`` must exactly match the requested customer; a
        cross-customer debtor is rejected and never yields an address.
        """
        customer_id = self._validate_id(customer_id, "customer_id")
        debtor = await self._get_debtor(company, customer_id)
        returned_code = self._pick(
            debtor, "accNo", "AccNo", "debtor code"
        )
        if returned_code != customer_id:
            raise AutoCountDataError(
                "AutoCount returned a different debtor for the requested customer"
            )
        address_text = self._normalise_address(
            debtor,
            address_keys=("deliverAddress", "DeliverAddress"),
            postcode_keys=("deliverPostCode", "DeliverPostCode"),
            label="delivery",
        )
        if not address_text:
            return []
        billing_address_text = self._normalise_address(
            debtor,
            address_keys=("address", "Address"),
            postcode_keys=("postCode", "PostCode"),
            label="billing",
        )
        return [
            DeliveryAddress(
                id=f"{customer_id}:delivery",
                label=DEFAULT_ADDRESS_LABEL,
                address_text=address_text,
                billing_address_text=billing_address_text,
            )
        ]

    async def get_customer(
        self, company: CompanyConfig, customer_id: str
    ) -> CustomerSummary:
        """Fetch one customer with the tax entity linked in its debtor master."""
        customer_id = self._validate_id(customer_id, "customer_id")
        debtor = await self._get_debtor(company, customer_id)
        code = self._pick(debtor, "accNo", "AccNo", "debtor code")
        if code != customer_id:
            raise AutoCountDataError(
                "AutoCount returned a different debtor for the requested customer"
            )
        return CustomerSummary(
            id=code,
            code=code,
            name=self._pick(debtor, "companyName", "CompanyName", "debtor name"),
            tax_entity=self._optional_pick(
                debtor, "taxEntity", "TaxEntity", "tax entity"
            ),
        )

    async def search_items(
        self, company: CompanyConfig, query: str
    ) -> list[ProductSummary]:
        """All active products whose code or name contains ``query`` (case-insensitive)."""
        self._validate_query(query)

        def body_for(page: int) -> dict[str, Any]:
            return {"page": page, "filter": {"statuses": _ACTIVE_STATUSES}}

        summaries = await self._listing(
            company,
            "POST",
            "product/listing",
            params_for=None,
            body_for=body_for,
            extract=self._product_row,
        )
        return self._filter(summaries, query)

    async def search_invoices(
        self,
        company: CompanyConfig,
        *,
        customer_id: str,
        date_from: str,
        date_to: str,
    ) -> list[InvoiceSummary]:
        """Invoices for one customer whose creation time falls in the window.

        Uses the documented invoice listing filter ``debtorCode`` (multiSelect)
        and ``createdDate`` (from/to) so only the narrow reconciliation window
        is fetched; line-level and total matching are the caller's job because
        the listing API has no item-level filter.

        Each row is the documented invoice view model directly (``master`` +
        ``details``, unlike product rows which wrap under ``product``). An
        invoice is normalised to ``InvoiceSummary``; the document key
        (``docKey``) is the stable identity, and the detail multiset is
        preserved exactly so a caller can match against a confirmed draft.
        """
        customer_id = self._validate_id(customer_id, "customer_id")
        date_from = self._validate_id(date_from, "date_from")
        date_to = self._validate_id(date_to, "date_to")

        def body_for(page: int) -> dict[str, Any]:
            return {
                "page": page,
                "filter": {
                    "debtorCode": {"multiSelect": [customer_id]},
                    "createdDate": {"from": date_from, "to": date_to},
                },
            }

        return await self._listing(
            company,
            "POST",
            "invoice/listing",
            params_for=None,
            body_for=body_for,
            extract=self._invoice_row,
        )

    async def list_recent_invoices(
        self,
        company: CompanyConfig,
        *,
        date_from: str,
        date_to: str,
    ) -> list[InvoiceSummary]:
        """Every invoice in the account book whose document date is in range.

        Uses the documented invoice listing ``date`` (docDate) filter with no
        debtor filter, so a caller browsing what was issued sees every
        customer's invoices. ``search_invoices`` keeps its ``debtorCode`` +
        ``createdDate`` filter untouched because price history and
        ambiguous-write reconciliation depend on that exact behaviour.

        Sorted newest first with the document number as a stable tie-break for
        same-day invoices, so a caller can render the order it receives.
        Cancelled invoices are included and flagged; presenting them is the
        caller's decision.

        Tolerates a record straddling two pages (``on_repeat="skip"``) rather
        than failing the whole browse. This window routinely spans more than
        one page -- the live book returned 126 invoices for three days against
        a 100-record page -- and AutoCount's listing has no stable order, so a
        concurrent write can shift the page boundary mid-read. Reconciliation
        keeps the strict guard; a browse list does not need to be exact to be
        useful, and an error in its place is strictly worse.
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
            on_repeat="skip",
        )
        return sorted(invoices, key=lambda i: (i.doc_date, i.doc_no), reverse=True)

    async def get_invoice(
        self, company: CompanyConfig, invoice_no: str
    ) -> InvoiceSummary:
        """One invoice by its AutoCount document number.

        ``GET /invoice?docNo=`` returns the same documented view model
        (``master`` + ``details``) as an invoice listing row, so it
        normalises through ``_invoice_row`` unchanged. The returned ``docNo``
        must match the requested one; a mismatch fails closed rather than
        silently handing back a different invoice, and the mismatched number
        is kept out of the message.

        Never cached: a caller reading an invoice back to check what
        AutoCount currently holds depends on this reflecting the account book
        right now.
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

    async def get_invoice_pdf(
        self, company: CompanyConfig, invoice_id: str
    ) -> bytes:
        """The official AutoCount invoice PDF, or fail closed as unsupported.

        Technical spike (see ``docs/autocount/pdf-spike.md``): the documented
        Cloud Accounting Integration API exposes no PDF, print, or download
        mechanism, so this contract raises ``AutoCountUnsupportedError``
        without making an HTTP call and sharing stays disabled.

        When AutoCount documents a supported mechanism, replace the raise with
        the documented call through ``AutoCountClient.read`` and validate the
        returned bytes start with ``%PDF`` before returning them; the unit
        tests already encode that intended behaviour.
        """
        self._validate_id(invoice_id, "invoice_id")
        raise AutoCountUnsupportedError(
            "AutoCount provides no documented invoice PDF endpoint; "
            "PDF sharing is disabled (see docs/autocount/pdf-spike.md)"
        )

    async def get_latest_price(
        self,
        company: CompanyConfig,
        customer_id: str,
        item_id: str,
        *,
        now: datetime | None = None,
    ) -> PriceHistory | None:
        """The latest unit price issued to this customer for this item.

        Searches the customer's invoices created within the last
        ``PRICE_HISTORY_WINDOW_DAYS`` days and returns the unit price of the
        most recent non-cancelled invoice whose details contain the item,
        together with that source invoice's number and date. Returns ``None``
        when no prior invoice for the pair exists in the window.

        ``now`` pins the window end for deterministic tests; the default is
        the current UTC time.
        """
        customer_id = self._validate_id(customer_id, "customer_id")
        item_id = self._validate_id(item_id, "item_id")
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)).isoformat()
        invoices = await self.search_invoices(
            company,
            customer_id=customer_id,
            date_from=since,
            date_to=now.isoformat(),
        )
        for invoice in sorted(
            (i for i in invoices if not i.is_cancelled),
            key=lambda i: i.doc_date,
            reverse=True,
        ):
            for line in invoice.lines:
                if line.product_code == item_id:
                    return PriceHistory(
                        item_id=item_id,
                        customer_id=customer_id,
                        unit_price=line.unit_price,
                        source_invoice_number=invoice.doc_no,
                        source_invoice_date=invoice.doc_date,
                    )
        return None

    async def get_item(
        self, company: CompanyConfig, item_id: str
    ) -> ProductSummary:
        """One product by its AutoCount lookup code.

        The returned ``productCode`` must exactly match ``item_id``.
        """
        item_id = self._validate_id(item_id, "item_id")
        response = await self._client.read(
            company, "GET", "product", params={"code": item_id}
        )
        payload = self._json_object(response, "AutoCount returned a malformed product payload")
        product = payload.get("product")
        if not isinstance(product, dict):
            raise AutoCountDataError(
                "AutoCount product payload is missing its product data"
            )
        code = self._product_code(product)
        if code != item_id:
            raise AutoCountDataError(
                "AutoCount returned a different product for the requested item"
            )
        return self._product_summary(product)

    async def _get_debtor(
        self, company: CompanyConfig, customer_id: str
    ) -> dict[str, Any]:
        response = await self._client.read(
            company, "GET", "debtor", params={"code": customer_id}
        )
        return self._json_object(response, "AutoCount returned a malformed debtor payload")

    async def _listing(
        self,
        company: CompanyConfig,
        method: str,
        endpoint: str,
        *,
        params_for: Callable[[int], dict[str, Any]] | None,
        body_for: Callable[[int], dict[str, Any]] | None,
        extract: Callable[[Any], Any],
        on_repeat: str = "fail",
    ) -> list[Any]:
        """Page through a documented listing until ``totalCount`` is consumed.

        Every page must declare the same ``totalCount`` as the first page;
        empty data before the total is consumed, overshooting the total, or
        running past the internal page ceiling all fail closed instead of
        being silently accepted or looped over forever.

        ``on_repeat`` decides what a record appearing on two pages means:

        ``"fail"`` (default)
            A repeat is a contract violation. Reconciliation matches invoices
            by identity, so a listing it cannot trust must not be used at all.

        ``"skip"``
            A repeat is a paging race, not corruption, and the duplicate is
            dropped. AutoCount's listing has no stable order -- an unfiltered
            page 1 comes back neither newest- nor oldest-first -- so a write
            landing between two page requests shifts the boundary and a row
            can straddle it. That is a snapshot artefact of a busy account
            book, and failing a browse over it means the operator sees an
            error instead of their invoices. Paging stops when a page
            contributes nothing new, since a page that only repeats what is
            already collected is not making progress.
        """
        collected: list[Any] = []
        seen: set[str] = set()
        total: int | None = None
        page = 1
        while True:
            response = await self._client.read(
                company,
                method,
                endpoint,
                params=params_for(page) if params_for else None,
                json=body_for(page) if body_for else None,
            )
            data, page_total = self._parse_listing_response(response)
            if total is None:
                total = page_total
            elif page_total != total:
                raise AutoCountDataError(
                    "AutoCount listing declared an inconsistent record total"
                )
            added = 0
            for row in data:
                item = extract(row)
                if item.id in seen:
                    if on_repeat == "fail":
                        raise AutoCountDataError(
                            "AutoCount listing repeated a record across pages"
                        )
                    continue
                seen.add(item.id)
                collected.append(item)
                added += 1
            if len(collected) > total:
                raise AutoCountDataError(
                    "AutoCount listing exceeded its declared record total"
                )
            if len(collected) == total:
                return collected
            if not data:
                if on_repeat == "skip":
                    return collected
                raise AutoCountDataError(
                    "AutoCount listing ended before its declared record total"
                )
            # A page that repeated everything it returned cannot be advanced
            # past; asking for the next one would loop on the same boundary.
            if added == 0:
                return collected
            if page >= _LISTING_PAGE_LIMIT:
                raise AutoCountDataError(
                    "AutoCount listing required more pages than allowed"
                )
            page += 1

    @staticmethod
    def _parse_listing_response(response: Any) -> tuple[list[Any], int]:
        payload = AutoCountMasterDataAdapter._json_object(
            response, "AutoCount returned a malformed listing payload"
        )
        data = payload.get("data")
        total = payload.get("totalCount")
        if not isinstance(data, list):
            raise AutoCountDataError(
                "AutoCount listing payload is missing its data array"
            )
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AutoCountDataError(
                "AutoCount listing payload has a malformed record total"
            )
        return data, total

    @staticmethod
    def _json_object(response: Any, message: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise AutoCountDataError("AutoCount returned a non-JSON payload") from None
        if not isinstance(payload, dict):
            raise AutoCountDataError(message)
        return payload

    @classmethod
    def _customer_row(cls, row: Any) -> CustomerSummary:
        if not isinstance(row, dict):
            raise AutoCountDataError("AutoCount debtor listing contains a malformed row")
        code = cls._pick(row, "accNo", "AccNo", "debtor code")
        name = cls._pick(row, "companyName", "CompanyName", "debtor name")
        return CustomerSummary(id=code, code=code, name=name)

    @classmethod
    def _normalise_address(
        cls,
        row: dict[str, Any],
        *,
        address_keys: tuple[str, str],
        postcode_keys: tuple[str, str],
        label: str,
    ) -> str:
        """Return one customer address with its postcode on a new line.

        Billing and delivery addresses are distinct fields in AutoCount's
        debtor and invoice master models. A blank optional billing address is
        represented as ``""`` so the invoice mapper can use the confirmed
        delivery address as its safe fallback; a blank delivery address still
        causes the existing address-selection gate to return no options.
        """
        address = cls._first_non_null(row, address_keys)
        if address is None or (isinstance(address, str) and not address.strip()):
            return ""
        if not isinstance(address, str):
            raise AutoCountDataError(f"AutoCount debtor {label} address is malformed")
        address_text = address.strip()

        postcode = cls._first_non_null(row, postcode_keys)
        if postcode is not None:
            if not isinstance(postcode, str):
                raise AutoCountDataError(f"AutoCount debtor {label} postcode is malformed")
            postcode = postcode.strip()
            if postcode and postcode not in address_text:
                address_text = f"{address_text}\n{postcode}"
        return address_text

    @staticmethod
    def _first_non_null(row: dict[str, Any], keys: tuple[str, str]) -> Any:
        for key in keys:
            value = row.get(key)
            if value is not None:
                return value
        return None

    @classmethod
    def _product_row(cls, row: Any) -> ProductSummary:
        if not isinstance(row, dict):
            raise AutoCountDataError("AutoCount product listing contains a malformed row")
        product = row.get("product")
        if not isinstance(product, dict):
            raise AutoCountDataError(
                "AutoCount product listing row is missing its product data"
            )
        return cls._product_summary(product)

    @classmethod
    def _product_summary(cls, product: dict[str, Any]) -> ProductSummary:
        code = cls._product_code(product)
        name = cls._product_name(product)
        price = cls._product_price(product)
        return ProductSummary(
            id=code,
            code=code,
            name=name,
            default_price=price,
            classification_code=cls._optional_pick(
                product,
                "classificationCode",
                "ClassificationCode",
                "classification code",
            ),
        )

    @staticmethod
    def _product_code(product: dict[str, Any]) -> str:
        code = product.get("productCode")
        if code is None or not isinstance(code, str) or not code.strip():
            raise AutoCountDataError("AutoCount product is missing its product code")
        return code.strip()

    @staticmethod
    def _product_name(product: dict[str, Any]) -> str:
        name = product.get("productName")
        if name is None or not isinstance(name, str) or not name.strip():
            raise AutoCountDataError("AutoCount product is missing its product name")
        return name.strip()

    @staticmethod
    def _product_price(product: dict[str, Any]) -> Decimal:
        raw = product.get("price")
        if raw is None or isinstance(raw, bool):
            raise AutoCountDataError("AutoCount product is missing a valid price")
        try:
            price = Decimal(str(raw))
        except (decimal.InvalidOperation, ValueError):
            raise AutoCountDataError("AutoCount product has a malformed price") from None
        if not price.is_finite() or price < 0:
            raise AutoCountDataError("AutoCount product has an unsafe price")
        return price

    @classmethod
    def _invoice_row(cls, row: Any) -> InvoiceSummary:
        if not isinstance(row, dict):
            raise AutoCountDataError("AutoCount invoice listing contains a malformed row")
        master = row.get("master")
        if not isinstance(master, dict):
            raise AutoCountDataError("AutoCount invoice row is missing its master data")
        doc_key = master.get("docKey")
        if (
            doc_key is None
            or isinstance(doc_key, bool)
            or not isinstance(doc_key, (int, str))
        ):
            raise AutoCountDataError("AutoCount invoice is missing its document key")
        invoice_id = str(doc_key).strip()
        if not invoice_id:
            raise AutoCountDataError("AutoCount invoice has a blank document key")
        details = row.get("details")
        if not isinstance(details, list):
            raise AutoCountDataError("AutoCount invoice row is missing its details")
        return InvoiceSummary(
            id=invoice_id,
            doc_no=cls._pick(master, "docNo", "DocNo", "document number"),
            doc_date=cls._pick(master, "docDate", "DocDate", "document date"),
            debtor_code=cls._pick(master, "debtorCode", "DebtorCode", "debtor code"),
            total=cls._invoice_total(master),
            lines=tuple(cls._invoice_detail(detail) for detail in details),
            is_cancelled=cls._invoice_cancelled(master),
            debtor_name=cls._optional_text(master, "debtorName", "DebtorName"),
            credit_term=cls._optional_text(master, "creditTerm", "CreditTerm"),
            sales_location=cls._optional_text(master, "salesLocation", "SalesLocation"),
        )

    @staticmethod
    def _optional_text(row: dict[str, Any], camel: str, pascal: str) -> str:
        """A documented two-casing header field, blank when absent.

        Lenient where ``_pick`` is strict: these are only needed when echoing
        a master back on an update, and a listing row that omits one must not
        break price history or reconciliation. A non-string is still a
        contract violation. The edit path rejects a blank before it can reach
        AutoCount as an empty mandatory field.
        """
        value = row.get(camel)
        if value is None:
            value = row.get(pascal)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise AutoCountDataError("AutoCount invoice header field is malformed")
        return value.strip()

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

    @staticmethod
    def _invoice_total(master: dict[str, Any]) -> Decimal:
        return AutoCountMasterDataAdapter._strict_decimal(
            master.get("total"), "invoice total", must_be_positive=False
        )

    @staticmethod
    def _invoice_cancelled(master: dict[str, Any]) -> bool:
        raw = master.get("cancelled")
        if raw is None:
            return False
        if not isinstance(raw, bool):
            raise AutoCountDataError("AutoCount invoice has a malformed cancelled flag")
        return raw

    @staticmethod
    def _strict_decimal(
        raw: Any, what: str, *, must_be_positive: bool
    ) -> Decimal:
        """Exact finite ``Decimal`` from a documented numeric field, fail closed."""
        if raw is None or isinstance(raw, bool):
            raise AutoCountDataError(f"AutoCount {what} is missing or invalid")
        try:
            value = Decimal(str(raw))
        except (decimal.InvalidOperation, ValueError):
            raise AutoCountDataError(f"AutoCount {what} is malformed") from None
        if not value.is_finite():
            raise AutoCountDataError(f"AutoCount {what} is not finite")
        if value < 0 or (must_be_positive and value <= 0):
            raise AutoCountDataError(f"AutoCount {what} is unsafe")
        return value

    @staticmethod
    def _pick(
        row: dict[str, Any], camel: str, pascal: str, what: str
    ) -> str:
        """Extract a documented two-casing field, rejecting conflicts.

        Accepts both documented casing forms (for example ``accNo`` and
        ``AccNo``). Both present but different, or a missing/blank/non-string
        value, is malformed.
        """
        camel_value = row.get(camel)
        pascal_value = row.get(pascal)
        if camel_value is not None and pascal_value is not None and camel_value != pascal_value:
            raise AutoCountDataError(f"AutoCount returned conflicting {what} fields")
        value = camel_value if camel_value is not None else pascal_value
        if value is None or not isinstance(value, str) or not value.strip():
            raise AutoCountDataError(f"AutoCount returned a malformed {what}")
        return value.strip()

    @staticmethod
    def _optional_pick(
        row: dict[str, Any], camel: str, pascal: str, what: str
    ) -> str | None:
        """Extract an optional text field while rejecting malformed conflicts."""
        camel_value = row.get(camel)
        pascal_value = row.get(pascal)
        if (
            camel_value is not None
            and pascal_value is not None
            and camel_value != pascal_value
        ):
            raise AutoCountDataError(f"AutoCount returned conflicting {what} fields")
        value = camel_value if camel_value is not None else pascal_value
        if value is None:
            return None
        if not isinstance(value, str):
            raise AutoCountDataError(f"AutoCount returned a malformed {what}")
        value = value.strip()
        return value or None

    @staticmethod
    def _validate_query(query: object) -> None:
        if not isinstance(query, str):
            raise ValueError("query must be a string")

    @staticmethod
    def _validate_id(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _filter(summaries: list[Any], query: str) -> list[Any]:
        needle = query.strip().lower()
        if not needle:
            return summaries
        return [
            s
            for s in summaries
            if needle in s.code.lower() or needle in s.name.lower()
        ]
