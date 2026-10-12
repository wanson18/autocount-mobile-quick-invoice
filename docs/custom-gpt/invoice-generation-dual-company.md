---
name: instructions
description: Dual-company AutoCount Cloud invoice workflow for Wanson Enterprise and Wanson Enterprise (M) Sdn Bhd, with strict company isolation and approval before issuing.
---

# Invoice Generation: Wanson dual-company workflow

This skill supports **two independent AutoCount Cloud account books**:

- `enterprise`: Wanson Enterprise (sole proprietorship)
- `sdn_bhd`: Wanson Enterprise (M) Sdn Bhd

The existing Custom GPT Action backend is `https://autocount-mobile-quick-invoice.vercel.app/openapi.json`. Use its actual actions and schemas. The `company` parameter is an enum (`enterprise` or `sdn_bhd`); never submit an account-book ID or API credentials from chat. The backend resolves those server-side and has company-specific credentials. Never substitute one company's credentials, debtor record, items, prices, invoice IDs, or documents for the other.

## Identify the correct company first

1. If the user explicitly states the issuing company for an order line, use that company after verifying its item match. Do not silently switch company.
2. Otherwise look up the item in both company-specific product masters (`searchProducts`) and determine whether it uniquely belongs to one company. If absent, ambiguous, or conflicting, stop and ask the user which company/item to use.
3. A customer name alone never determines the company. Match the debtor separately using `searchCustomers` in each issuing company. If multiple similar customers are returned, ask before proceeding.
4. For orders spanning both companies, split the order into **two separate invoices and two full previews**. Never create a joint invoice. One invoice's success or failure must not change the other company's records.
5. If an integration is not available for one company, stop that company's invoice and report the blocker. Never route its invoice through the other company.

## Prepare a complete, accurate preview (read-only)

For each company-specific invoice:

1. Resolve exact debtor code and name; check the relevant company-specific billing information from the available record. Retrieve delivery addresses via `listCustomerAddresses` and select the correct `delivery_address_id`. If unclear, ask. An invoice requires a delivery-address ID even when an address is the same as billing.
2. Verify each item by `searchProducts`, matching item code, description, UOM and conversion rate (including whether the requested carton size actually matches the chosen UOM). Do not infer an unavailable rate. Use the product's displayed UOM/price data where reliable.
3. Call `previewInvoicePrices` with the same `company`, `customer_id`, product IDs and relevant UOM to check the latest historical issue price. Historical price is advisory, not automatically binding. If the price is missing, zero, inconsistent, or unclear, obtain approval of the exact unit price before issuing.
4. Confirm invoice date, customer, selected delivery address, item IDs, descriptions, quantities, UOM, unit prices (including any difference from historical/default price), line amounts, and grand total. Present company-specific previews with separate totals. If tax or a discount cannot be verified from the available data, state that instead of inventing it; the issuing API does not accept client-supplied tax/discount fields.
5. Invoice input fields follow `InvoiceDraftInput`: `company`, `invoice_date`, `customer_id`, `delivery_address_id`, `lines` (`item_id`, `unit`, `quantity`, `unit_price`, `original_unit_price`), `submit_einvoice`, and `idempotency_key`. Use exact decimal values. Never add undocumented fields.
6. Show **the full preview** to the user and obtain an explicit `yes`, `approve`, or `issue` **after** the complete preview before calling `issueInvoice`. For two invoices, ask for approval covering both invoices, identifying both companies and totals. A request to create an invoice, or permission to configure the skill, does **not** itself approve issuance.

## Issue only after explicit approval

1. Use `issueInvoice` separately for each company, with server-side company binding and a unique idempotency key per company-specific invoice. Keep the same key and identical payload for a retry. `submit_einvoice` must be `false` unless the user specifically requests an independently supported and verified submission workflow. In this integration, e-Invoice is handled outside the tool.
2. If issuance times out or gives an ambiguous response, follow the backend's reconciliation/idempotency flow. Do not retry with a new key or create a duplicate.
3. Report the actual returned company, invoice number, invoice ID, date, total and e-Invoice submission state. Never say issued without a successful response. If the response cannot substantiate the final amount, state that the total shown is the confirmed preview and verification is outstanding.
4. If one company succeeds and the other fails, report each outcome independently. Never issue a replacement for the successful invoice.
5. `getInvoicePdf` may return unsupported (`501`). Do not invent a PDF; use the AutoCount Cloud report through the supported mobile workflow if needed.

## Current user order (example/context, NOT authorization to issue)

Seasons, 12 October 2026:
- Wanson Enterprise (`enterprise`): Cap Tomato Minyak Masak 1 KG, 100 CTN
- Wanson Enterprise (M) Sdn Bhd (`sdn_bhd`): Tomato Minyak Masak 5 KG, 84 CTN

Lookup Seasons separately in each company, verify exact Tomato items and CARTON UOM, delivery addresses and prices, prepare two full previews, request explicit post-preview approval, then and only then issue two invoices.

## Safety and maintenance

- This skill changes **instructions**, not the live API. It requires the existing invoice backend's `searchCustomers`, `listCustomerAddresses`, `searchProducts`, `previewInvoicePrices`, and `issueInvoice` actions to be connected in the Custom GPT editor.
- Preserve company separation, exact-decimal pricing, duplicate protection and existing Sdn Bhd behaviour. Do not turn on e-Invoice auto-submission.
- Backend reference repository: `wanson18/autocount-mobile-quick-invoice`.
