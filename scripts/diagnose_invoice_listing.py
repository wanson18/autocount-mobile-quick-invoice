"""Read-only diagnosis of AutoCount's invoice listing paging.

Answers why ``list_recent_invoices`` failed live with "AutoCount listing
repeated a record across pages". Three causes are indistinguishable without
live data, and this separates them:

    A  ``page`` is ignored      -> page 2 returns exactly page 1's records
    B  ordering is unstable     -> pages overlap partially, differently each run
    C  the date filter is loose -> totalCount is far larger than the window

WRITES NOTHING. Only ``POST /invoice/listing`` (a read, despite the verb) and
no create, update, or delete of any kind. Safe to run against a live account
book as often as needed.

USAGE
-----
    export AUTOCOUNT_API_KEY_ID=... AUTOCOUNT_API_KEY=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_SDN_BHD=...
    export AUTOCOUNT_ACCOUNT_BOOK_WANSON_ENTERPRISE=placeholder-not-configured
    python scripts/diagnose_invoice_listing.py --company sdn_bhd --days 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from typing import Any

from app.autocount.client import AutoCountClient
from app.config import CompanyConfig, CompanyConfigError, get_company
from app.dependencies import ENV_API_KEY, ENV_KEY_ID
from app.models.company import CompanyKey


def log(message: str) -> None:
    print(message, flush=True)


def build_client() -> AutoCountClient:
    return AutoCountClient(
        os.environ.get(ENV_KEY_ID, ""), os.environ.get(ENV_API_KEY, "")
    )


def doc_keys(payload: dict) -> list[str]:
    """The docKey of every row, in the order AutoCount returned them."""
    out = []
    for row in payload.get("data") or []:
        master = (row or {}).get("master") or {}
        out.append(str(master.get("docKey", "?")))
    return out


def doc_nos(payload: dict) -> list[str]:
    out = []
    for row in payload.get("data") or []:
        master = (row or {}).get("master") or {}
        out.append(str(master.get("docNo", "?")))
    return out


def doc_dates(payload: dict) -> list[str]:
    out = []
    for row in payload.get("data") or []:
        master = (row or {}).get("master") or {}
        out.append(str(master.get("docDate", "?"))[:10])
    return out


async def fetch_page(
    client: AutoCountClient,
    company: CompanyConfig,
    page: int,
    body_filter: dict[str, Any] | None,
) -> dict:
    body: dict[str, Any] = {"page": page}
    if body_filter is not None:
        body["filter"] = body_filter
    response = await client.read(company, "POST", "invoice/listing", json=body)
    return response.json()


async def diagnose(company: CompanyConfig, days: int) -> int:
    client = build_client()
    try:
        today = date.today()
        window = {
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        }

        log("=" * 72)
        log(f"AutoCount invoice listing diagnosis -- {company.key.value}")
        log(f"window: {window['from']} .. {window['to']}  ({days} days)")
        log("=" * 72)

        # --- 1. unfiltered, for a baseline on book size -------------------
        log("\n[1] UNFILTERED listing, page 1 (baseline book size)")
        unfiltered = await fetch_page(client, company, 1, None)
        unfiltered_total = unfiltered.get("totalCount")
        log(f"    totalCount        : {unfiltered_total}")
        log(f"    rows on page 1    : {len(doc_keys(unfiltered))}")
        dates = doc_dates(unfiltered)
        if dates:
            log(f"    first/last docDate: {dates[0]} .. {dates[-1]}")
            log(f"    first/last docNo  : {doc_nos(unfiltered)[0]} .. {doc_nos(unfiltered)[-1]}")

        # --- 2. filtered by the documented date filter --------------------
        log(f"\n[2] DATE-FILTERED listing, page 1  filter={{'date': {window}}}")
        p1 = await fetch_page(client, company, 1, {"date": window})
        total = p1.get("totalCount")
        keys1 = doc_keys(p1)
        log(f"    totalCount        : {total}")
        log(f"    rows on page 1    : {len(keys1)}")
        dates1 = doc_dates(p1)
        if dates1:
            log(f"    first/last docDate: {dates1[0]} .. {dates1[-1]}")
            log(f"    first/last docNo  : {doc_nos(p1)[0]} .. {doc_nos(p1)[-1]}")

        if unfiltered_total == total:
            log("    >>> C: the date filter did NOT narrow the result set")
        else:
            log("    >>> the date filter DID narrow the result set")

        # --- 3. does the window actually hold what it claims? -------------
        outside = [d for d in dates1 if d and d != "?" and not (window["from"] <= d <= window["to"])]
        if outside:
            log(f"    >>> rows OUTSIDE the requested window: {len(outside)} "
                f"(e.g. {outside[:3]})")
        else:
            log("    >>> every row on page 1 falls inside the window")

        # --- 4. does page 2 differ from page 1? ---------------------------
        if total is None or len(keys1) >= (total or 0):
            log("\n[3] SKIPPED page-2 comparison: one page already covers totalCount")
            log("\nCONCLUSION: with this window the listing fits in a single page,")
            log("so the paging loop is never entered and cannot duplicate.")
            return 0

        log("\n[3] PAGE 2 comparison (this is where the live error came from)")
        p2 = await fetch_page(client, company, 2, {"date": window})
        keys2 = doc_keys(p2)
        log(f"    rows on page 2    : {len(keys2)}")
        overlap = set(keys1) & set(keys2)
        log(f"    docKeys on both   : {len(overlap)}")

        if keys1 == keys2:
            log("    >>> A: page 2 is IDENTICAL to page 1 -- 'page' is ignored")
        elif overlap:
            log(f"    >>> B: pages OVERLAP partially ({len(overlap)} shared) -- "
                "ordering is unstable across requests")
        else:
            log("    >>> pages are disjoint -- paging itself looks correct")

        # --- 5. is the ordering stable when the same page is refetched? ---
        log("\n[4] STABILITY: refetching page 1 to see if the order changes")
        p1b = await fetch_page(client, company, 1, {"date": window})
        keys1b = doc_keys(p1b)
        if keys1b == keys1:
            log("    >>> page 1 returned the same rows in the same order")
        elif set(keys1b) == set(keys1):
            log("    >>> B: same rows, DIFFERENT order -- ordering is unstable")
        else:
            log("    >>> B: different rows entirely -- ordering is unstable")

        log("\n" + "=" * 72)
        log("Paste this whole block back to Claude.")
        log("=" * 72)
        return 0
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--company", required=True, choices=[k.value for k in CompanyKey]
    )
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    try:
        company = get_company(CompanyKey(args.company))
    except CompanyConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 4

    if not os.environ.get(ENV_KEY_ID) or not os.environ.get(ENV_API_KEY):
        if not (company.key_id and company.api_key):
            print("Missing AutoCount credentials.", file=sys.stderr)
            return 4

    return asyncio.run(diagnose(company, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
