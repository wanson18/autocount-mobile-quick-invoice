"""Live spike: can an approved AutoCount invoice be edited via PUT?

Answers the three undocumented questions that
``docs/specs/2026-08-13-view-edit-invoices-design.md`` depends on. If Q1 comes
back "no", the whole view-and-edit design is void and the feature has to
become void-and-reissue instead, so this runs before any implementation.

    Q1  Does an invoice created with ``saveApprove: true`` accept ``PUT`` at all?
    Q2  Does omitting ``master`` from the PUT body preserve the header?
    Q3  Does a shorter ``details`` array delete the trailing rows?

WHAT THIS DOES TO YOUR LIVE ACCOUNT BOOK
----------------------------------------
It creates ONE throwaway invoice, edits it twice, then deletes it. It never
touches any invoice it did not create -- every mutation asserts the document
number against the one it created moments earlier, and it aborts if that
check ever fails.

If the run dies partway (network drop, Ctrl-C), the throwaway invoice may
survive. The script prints its document number as soon as it exists, so you
can delete it by hand in AutoCount. Run with ``--cleanup-only <docNo>`` to
delete a leftover from a previous run.

USAGE
-----
Credentials come from the environment, exactly as the server reads them
(see ``.env.example``)::

    export AUTOCOUNT_API_KEY_ID=...
    export AUTOCOUNT_API_KEY=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD=...

    python scripts/spike_invoice_update.py --company sdn_bhd --confirm

Both account-book variables are needed even though only one company is
spiked: ``app.config`` validates the whole mapping at once.

Add ``--customer <code>`` and ``--item <code>`` (repeatable) to pin the master
data instead of letting the script pick the first active records it finds.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.autocount.client import AutoCountClient
from app.autocount.errors import AutoCountRejectedError
from app.config import CompanyConfig, CompanyConfigError, get_company
from app.dependencies import ENV_API_KEY, ENV_KEY_ID
from app.models.company import CompanyKey

SPIKE_REMARK = "SPIKE - safe to delete - invoice update capability test"
ACC_NO = "500-0000"
CREDIT_TERM = "C.O.D."
SALES_LOCATION = "HQ"


class SpikeAborted(RuntimeError):
    """The spike stopped deliberately rather than risk touching live data."""


def build_client() -> AutoCountClient:
    """Same credential source the server uses (see app/dependencies.py).

    Per-company credential overrides are honoured by the client itself via
    ``CompanyConfig``, so only the client-wide pair is needed here.
    """
    return AutoCountClient(
        os.environ.get(ENV_KEY_ID, ""),
        os.environ.get(ENV_API_KEY, ""),
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def doc_no_from_location(response: Any) -> str:
    """Recover the created invoice's docNo from the ``location`` header.

    Create Invoice returns a bare 201 with no guaranteed JSON body; the
    location header is the only documented success signal. Same parsing as
    ``InvoiceService._resolve_created_invoice``.
    """
    location = response.headers.get("location")
    if not location or not location.strip():
        raise SpikeAborted(
            "create returned no location header, so the new invoice cannot be "
            "identified -- check AutoCount for a stray invoice before rerunning"
        )
    doc_no = parse_qs(urlsplit(location).query).get("docNo", [None])[0]
    if not doc_no or not doc_no.strip():
        raise SpikeAborted(
            "create location header carried no docNo -- check AutoCount for a "
            "stray invoice before rerunning"
        )
    return doc_no.strip()


async def get_invoice(client: AutoCountClient, company: CompanyConfig, doc_no: str) -> dict:
    response = await client.read(company, "GET", "invoice", params={"docNo": doc_no})
    payload = response.json()
    if not isinstance(payload, dict):
        raise SpikeAborted(f"get-invoice for {doc_no} returned a non-object payload")
    return payload


def summarise_lines(payload: dict) -> list[tuple[str, str, str]]:
    details = payload.get("details") or []
    out = []
    for detail in details:
        out.append(
            (
                str(detail.get("productCode", "?")),
                str(detail.get("qty", "?")),
                str(detail.get("unitPrice", "?")),
            )
        )
    return out


def summarise_header(payload: dict) -> dict[str, str]:
    master = payload.get("master") or {}
    keys = (
        "docNo",
        "docDate",
        "debtorCode",
        "debtorName",
        "deliverAddress",
        "creditTerm",
        "salesLocation",
    )
    return {k: str(master.get(k, "<absent>")) for k in keys}


def detail_row(product_code: str, description: str, qty: Decimal, price: Decimal) -> dict:
    """One fully-specified detail row, matching what the create path sends.

    ``qty``/``unitPrice`` stay ``Decimal``: the client encodes them as exact
    bare JSON numbers. A quoted string fails with a System.Decimal error and
    a float would risk silent rounding.
    """
    return {
        "productCode": product_code,
        "description": description,
        "qty": qty,
        "unitPrice": price,
        "accNo": ACC_NO,
    }


# --------------------------------------------------------------------------
# master data discovery
# --------------------------------------------------------------------------


async def first_active_customer(client: AutoCountClient, company: CompanyConfig) -> tuple[str, str]:
    response = await client.read(
        company, "GET", "debtor/listing", params={"page": 1, "activeOnly": "true"}
    )
    rows = response.json().get("data") or []
    for row in rows:
        code = row.get("accNo") or row.get("AccNo")
        name = row.get("companyName") or row.get("CompanyName")
        if code and name:
            return str(code).strip(), str(name).strip()
    raise SpikeAborted("no active customer found; pass --customer <code> explicitly")


async def first_active_items(
    client: AutoCountClient, company: CompanyConfig, wanted: int
) -> list[tuple[str, str, Decimal]]:
    response = await client.read(
        company,
        "POST",
        "product/listing",
        json={
            "page": 1,
            "filter": {"statuses": {"active": True, "inactive": False, "discontinued": False}},
        },
    )
    rows = response.json().get("data") or []
    out: list[tuple[str, str, Decimal]] = []
    for row in rows:
        product = row.get("product") or {}
        code = product.get("productCode")
        name = product.get("productName")
        price = product.get("price")
        if code and name and price is not None:
            out.append((str(code).strip(), str(name).strip(), Decimal(str(price))))
        if len(out) == wanted:
            return out
    if not out:
        raise SpikeAborted("no active product found; pass --item <code> explicitly")
    # Fewer distinct products than requested: repeat the last one. Three
    # detail rows is what matters, not three distinct products.
    while len(out) < wanted:
        out.append(out[-1])
    return out


async def resolve_item(
    client: AutoCountClient, company: CompanyConfig, code: str
) -> tuple[str, str, Decimal]:
    response = await client.read(company, "GET", "product", params={"code": code})
    product = (response.json() or {}).get("product") or {}
    name = product.get("productName")
    price = product.get("price")
    if not name or price is None:
        raise SpikeAborted(f"product {code!r} not found or has no price")
    return code, str(name).strip(), Decimal(str(price))


# --------------------------------------------------------------------------
# the spike
# --------------------------------------------------------------------------


async def run_spike(
    company: CompanyConfig,
    customer_code: str | None,
    item_codes: list[str],
) -> int:
    client = build_client()
    findings: dict[str, str] = {}
    created_doc_no: str | None = None

    try:
        # ---- master data ------------------------------------------------
        if customer_code:
            debtor_code, debtor_name = customer_code, customer_code
            log("setup", f"using customer {debtor_code} (from --customer)")
        else:
            debtor_code, debtor_name = await first_active_customer(client, company)
            log("setup", f"picked first active customer: {debtor_code} ({debtor_name})")

        if item_codes:
            items = [await resolve_item(client, company, c) for c in item_codes]
        else:
            items = await first_active_items(client, company, 3)
        while len(items) < 3:
            items.append(items[-1])
        items = items[:3]
        log("setup", f"using items: {', '.join(code for code, _, _ in items)}")

        # ---- create the throwaway invoice, approved ----------------------
        create_payload = {
            "master": {
                "docDate": date.today().isoformat(),
                "debtorCode": debtor_code,
                "debtorName": debtor_name,
                "deliverAddress": "SPIKE TEST - DELETE ME",
                "creditTerm": CREDIT_TERM,
                "salesLocation": SALES_LOCATION,
                "description": SPIKE_REMARK,
                "submitEInvoice": False,
                "submitConsolidatedEInvoice": False,
            },
            "details": [
                detail_row(code, name, Decimal("1"), price)
                for code, name, price in items
            ],
            "autoFillOption": {
                "accNo": True,
                "taxCode": True,
                "tariffCode": True,
                "localTotalCost": True,
            },
            # The whole point: approved, exactly as the app issues invoices.
            "saveApprove": True,
        }
        log("create", "creating throwaway invoice with saveApprove: true ...")
        create_response = await client.write(company, "POST", "invoice", json=create_payload)
        created_doc_no = doc_no_from_location(create_response)
        print()
        log("create", f"*** CREATED {created_doc_no} -- delete this by hand if the run dies ***")
        print()

        original = await get_invoice(client, company, created_doc_no)
        original_header = summarise_header(original)
        original_lines = summarise_lines(original)
        log("create", f"header: {original_header}")
        log("create", f"{len(original_lines)} lines: {original_lines}")
        if len(original_lines) != 3:
            log("warn", f"expected 3 lines back, got {len(original_lines)} -- Q3 may be unreadable")

        def guard(doc_no: str) -> None:
            """Never mutate anything but the invoice this run created."""
            if doc_no != created_doc_no:
                raise SpikeAborted(f"refusing to mutate {doc_no!r}, not created by this run")

        # ---- Q1: does an approved invoice accept PUT at all? -------------
        print()
        log("Q1", "PUT with the identical 3-row details array (no master key) ...")
        guard(created_doc_no)
        unchanged = {
            "details": [
                detail_row(code, name, Decimal("1"), price)
                for code, name, price in items
            ]
        }
        try:
            put_response = await client.write(
                company, "PUT", "invoice", params={"docNo": created_doc_no}, json=unchanged
            )
        except AutoCountRejectedError as exc:
            findings["Q1"] = f"NO -- AutoCount rejected the PUT (status {exc.status_code}): {exc}"
            log("Q1", findings["Q1"])
            findings["Q2"] = "not reached (Q1 failed)"
            findings["Q3"] = "not reached (Q1 failed)"
            return await finish(client, company, created_doc_no, findings)

        findings["Q1"] = f"YES -- approved invoice accepted PUT (status {put_response.status_code})"
        log("Q1", findings["Q1"])

        # ---- Q2: is the header preserved when master is omitted? ---------
        after_q1 = await get_invoice(client, company, created_doc_no)
        header_after = summarise_header(after_q1)
        drifted = {
            k: (original_header[k], header_after[k])
            for k in original_header
            if original_header[k] != header_after[k]
        }
        if drifted:
            findings["Q2"] = f"NO -- header fields changed when master was omitted: {drifted}"
        else:
            findings["Q2"] = "YES -- every header field survived a PUT with no master key"
        log("Q2", findings["Q2"])

        lines_after_q1 = summarise_lines(after_q1)
        log("Q2", f"lines after identical PUT: {len(lines_after_q1)} -> {lines_after_q1}")

        # ---- Q3: does a shorter details array delete trailing rows? ------
        print()
        log("Q3", "PUT with only 2 rows (row 1 and row 3) ...")
        guard(created_doc_no)
        shortened = {
            "details": [
                detail_row(items[0][0], items[0][1], Decimal("1"), items[0][2]),
                detail_row(items[2][0], items[2][1], Decimal("1"), items[2][2]),
            ]
        }
        await client.write(
            company, "PUT", "invoice", params={"docNo": created_doc_no}, json=shortened
        )
        after_q3 = await get_invoice(client, company, created_doc_no)
        lines_after_q3 = summarise_lines(after_q3)
        log("Q3", f"lines now: {len(lines_after_q3)} -> {lines_after_q3}")
        if len(lines_after_q3) == 2:
            expected = [items[0][0], items[2][0]]
            got = [code for code, _, _ in lines_after_q3]
            if got == expected:
                findings["Q3"] = (
                    "YES -- 3 rows became 2; surviving rows are row1 and row3 as sent. "
                    "Full-state replace works exactly as the design assumes."
                )
            else:
                findings["Q3"] = (
                    f"PARTIAL -- row count dropped to 2 but contents are {got}, expected {expected}"
                )
        elif len(lines_after_q3) == 3:
            findings["Q3"] = (
                "NO -- still 3 rows. A shorter details array does NOT delete the "
                "trailing row; removal needs a different mechanism."
            )
        else:
            findings["Q3"] = f"UNEXPECTED -- row count is {len(lines_after_q3)}"
        log("Q3", findings["Q3"])

        return await finish(client, company, created_doc_no, findings)

    except SpikeAborted as exc:
        print()
        log("abort", str(exc))
        if created_doc_no:
            log("abort", f"invoice {created_doc_no} may still exist -- delete it in AutoCount")
        return 2
    except Exception as exc:  # noqa: BLE001 - spike script, surface everything
        print()
        log("error", f"{type(exc).__name__}: {exc}")
        if created_doc_no:
            log("error", f"invoice {created_doc_no} may still exist -- delete it in AutoCount")
        return 3
    finally:
        await client.aclose()


async def finish(
    client: AutoCountClient,
    company: CompanyConfig,
    doc_no: str,
    findings: dict[str, str],
) -> int:
    """Delete the throwaway invoice, then print the findings."""
    print()
    log("cleanup", f"deleting throwaway invoice {doc_no} ...")
    try:
        await client.write(company, "DELETE", "invoice", params={"docNo": doc_no})
        log("cleanup", f"deleted {doc_no}")
        findings["cleanup"] = f"deleted {doc_no} via DELETE /invoice"
    except Exception as exc:  # noqa: BLE001
        log("cleanup", f"DELETE FAILED: {type(exc).__name__}: {exc}")
        log("cleanup", f">>> DELETE {doc_no} BY HAND IN AUTOCOUNT <<<")
        findings["cleanup"] = f"FAILED to delete {doc_no} ({exc}) -- delete it by hand"

    print()
    print("=" * 72)
    print("SPIKE FINDINGS -- paste this back to Claude")
    print("=" * 72)
    print(f"account book : {company.key.value}")
    print(f"invoice used : {doc_no}")
    for key in ("Q1", "Q2", "Q3", "cleanup"):
        if key in findings:
            print(f"{key:<8}: {findings[key]}")
    print("=" * 72)
    return 0 if findings.get("Q1", "").startswith("YES") else 1


async def cleanup_only(company: CompanyConfig, doc_no: str) -> int:
    client = build_client()
    try:
        log("cleanup", f"deleting {doc_no} ...")
        await client.write(company, "DELETE", "invoice", params={"docNo": doc_no})
        log("cleanup", f"deleted {doc_no}")
        return 0
    except Exception as exc:  # noqa: BLE001
        log("cleanup", f"failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--company",
        required=True,
        choices=[k.value for k in CompanyKey],
        help="which account book to run against",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: acknowledges this creates and deletes a real invoice",
    )
    parser.add_argument("--customer", help="customer/debtor code to use")
    parser.add_argument("--item", action="append", default=[], help="item code (repeatable)")
    parser.add_argument("--cleanup-only", metavar="DOCNO", help="just delete a leftover invoice")
    args = parser.parse_args()

    # config._load validates BOTH account books at once, so both account-book
    # env vars must be set even when only one company is being spiked.
    try:
        company = get_company(CompanyKey(args.company))
    except CompanyConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print("Required environment (see .env.example):", file=sys.stderr)
        for var in (
            ENV_KEY_ID,
            ENV_API_KEY,
            "AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE",
            "AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD",
        ):
            print(f"  {var}={'SET' if os.environ.get(var) else 'MISSING'}", file=sys.stderr)
        return 4

    if not os.environ.get(ENV_KEY_ID) or not os.environ.get(ENV_API_KEY):
        if not (company.key_id and company.api_key):
            print(
                f"Missing AutoCount credentials: set {ENV_KEY_ID} and {ENV_API_KEY}, "
                f"or the per-company pair for {args.company}.",
                file=sys.stderr,
            )
            return 4

    if args.cleanup_only:
        return asyncio.run(cleanup_only(company, args.cleanup_only))

    if not args.confirm:
        print(__doc__)
        print("Refusing to run without --confirm.")
        print()
        print(f"This will create, edit, and delete ONE real invoice in: {company.name}")
        return 4

    print()
    print(f"Running against LIVE account book: {company.name} ({args.company})")
    print("Creating one throwaway invoice, editing it twice, then deleting it.")
    print()
    return asyncio.run(run_spike(company, args.customer, args.item))


if __name__ == "__main__":
    raise SystemExit(main())
