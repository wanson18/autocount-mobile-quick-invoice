"""Read-only look at where AutoCount keeps a product's units of measure.

The documented Get Product response shows only a base ``unit`` and a
``unitType``. This prints what a real product, a real product *listing* row
and a real invoice line actually carry, so the multi-UOM setup can be located
(or ruled out) before any selector is built on it.

WRITES NOTHING. Only ``GET /product``, ``POST /product/listing`` and
``POST /invoice/listing`` -- reads, despite the verb.

Output is structure plus unit-related values only (keys, array shapes, and the
value of any field whose name mentions unit / uom / pack / rate / barcode).
Names, prices and customers are not printed.

USAGE
-----
    export AUTOCOUNT_API_KEY_ID=... AUTOCOUNT_API_KEY=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE=placeholder-not-configured
    python scripts/diagnose_product_uom.py --company sdn_bhd --code <PRODUCT-CODE>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import date, timedelta
from typing import Any

from app.autocount.client import AutoCountClient
from app.config import CompanyConfig, CompanyConfigError, get_company
from app.dependencies import ENV_API_KEY, ENV_KEY_ID
from app.models.company import CompanyKey

UNITISH = re.compile(r"unit|uom|pack|rate|barcode", re.IGNORECASE)


def log(message: str = "") -> None:
    print(message, flush=True)


def describe(label: str, value: Any, indent: int = 4) -> None:
    """Print a JSON value's shape; values only where the key looks unit-related."""
    pad = " " * indent
    if isinstance(value, dict):
        log(f"{pad}{label}: object with {len(value)} keys")
        for key, inner in value.items():
            if isinstance(inner, (dict, list)):
                describe(key, inner, indent + 4)
            elif UNITISH.search(key):
                log(f"{pad}    {key} = {inner!r}")
            else:
                log(f"{pad}    {key}  ({type(inner).__name__})")
    elif isinstance(value, list):
        log(f"{pad}{label}: array of {len(value)}")
        if value:
            describe("[0]", value[0], indent + 4)
            for extra in value[1:3]:
                if isinstance(extra, dict):
                    shown = {k: v for k, v in extra.items() if UNITISH.search(k)}
                    log(f"{pad}    (next) {shown}")
    else:
        log(f"{pad}{label} = {value!r}")


def build_client() -> AutoCountClient:
    return AutoCountClient(
        os.environ.get(ENV_KEY_ID, ""), os.environ.get(ENV_API_KEY, "")
    )


async def diagnose(company: CompanyConfig, code: str) -> int:
    client = build_client()
    try:
        log("=" * 72)
        log(f"AutoCount product UOM diagnosis -- {company.key.value}, item {code}")
        log("=" * 72)

        log("\n[1] GET /product?code=...  (the full product)")
        response = await client.read(company, "GET", "product", params={"code": code})
        product = response.json()
        describe("response", product)

        log("\n[2] POST /product/listing, first row for the same code")
        page = 1
        found = None
        while page <= 10 and found is None:
            listing = (
                await client.read(company, "POST", "product/listing", json={"page": page})
            ).json()
            rows = listing.get("data") or []
            for row in rows:
                if ((row or {}).get("product") or {}).get("productCode") == code:
                    found = row
                    break
            if not rows or len(rows) < 1:
                break
            page += 1
        if found is None:
            log("    (code not found in the first 10 listing pages)")
        else:
            describe("listing row", found)

        log("\n[3] Newest invoice lines for the same code (last 30 days)")
        today = date.today()
        body = {
            "page": 1,
            "filter": {
                "createdDate": {
                    "from": (today - timedelta(days=30)).isoformat(),
                    "to": today.isoformat(),
                }
            },
        }
        invoices = (await client.read(company, "POST", "invoice/listing", json=body)).json()
        shown = 0
        for row in invoices.get("data") or []:
            for detail in (row or {}).get("details") or []:
                if detail.get("productCode") == code and shown < 3:
                    describe("invoice line", detail)
                    shown += 1
        if not shown:
            log("    (no invoice line for this code in the last 30 days)")
            # Fall back to any recent line whose unit differs from the product's
            # base unit: it shows how AutoCount stores a non-base unit.
            base = (product.get("product") or {}).get("unit")
            for row in invoices.get("data") or []:
                for detail in (row or {}).get("details") or []:
                    if shown < 2 and detail.get("unit") not in (None, "", base):
                        describe("recent line in another unit", detail)
                        shown += 1
            if not shown:
                log("    (no recent line in a non-base unit either)")
        return 0
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--company", required=True, choices=[c.value for c in CompanyKey])
    parser.add_argument("--code", required=True, help="AutoCount product code")
    args = parser.parse_args()
    try:
        company = get_company(CompanyKey(args.company))
    except CompanyConfigError as exc:
        log(f"configuration error: {exc}")
        return 2
    return asyncio.run(diagnose(company, args.code))


if __name__ == "__main__":
    sys.exit(main())
