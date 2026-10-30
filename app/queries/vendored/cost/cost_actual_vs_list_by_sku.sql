-- query_id: cost_actual_vs_list_by_sku
-- title: Actual vs list price by SKU (discount realization) -- not assessable
-- domain: cost   tier: deep
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices are generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: system.billing has only usage, list_prices and attributed_usage, and none of the three carries a negotiated or account-specific rate (DEC-66.1). pricing.effective_list.default is the effective (post-promotion) list price -- itself only a list-price estimate, never what the account actually pays, so an "actual vs list" comparison is not assessable from this data. This query always reads NOT_ASSESSED.
-- read_this: One row = a workspace + cloud + SKU + usage_unit's DBU spend for the window, priced for reference only: net_list_cost (list_prices.pricing.effective_list.default, DEC-66.1's one effective-list-price basis used everywhere else in this app). It is not a negotiated or invoiced rate, so status is always NOT_ASSESSED -- see not_assessed_reason.
-- healthy: n/a for this query - no negotiated-rate source exists anywhere in system.billing, so "actual vs list" can never be computed here; every row reads NOT_ASSESSED (see not_assessed_reason), never a pass.
-- investigate_if: nothing - this check cannot be assessed on any account until a negotiated-rate system table exists. Use net_list_cost (the effective list price, DEC-66.1) from cost_dollarized_by_sku_day for spend ranking instead.
-- actions: n/a - not assessable (no negotiated-rate source exists in system.billing)
-- next: cost_dollarized_by_sku_day (for the full dollarized cost series across all SKUs, at the same effective-list basis), cost_account_prices_raw (for the underlying raw price rows behind this comparison)
-- not_assessed_reasons: no_negotiated_rate_source: there is no negotiated-rate source anywhere in the account's billing data, only the effective list price
-- caveats: net_list_cost comes from system.billing.list_prices, never a negotiated-rate table - system.billing has only usage, list_prices and attributed_usage, and none of the three carries a contracted/account rate. It is priced at pricing.effective_list.default (the effective, post-promotion list price - DEC-66.1's single basis, used everywhere else in this app), for reference only; there is no second price column to compare it against, because a "realization ratio" between two list-price estimates would not be a real discount signal - that is why this query always emits status = NOT_ASSESSED with not_assessed_reason = 'no_negotiated_rate_source' rather than a fabricated band. The join is scoped to the price row that was active on each usage_date: active rows carry price_end_time = NULL, so the window predicate is (price_end_time IS NULL OR usage_date < DATE(price_end_time)) - using only usage_date < DATE(price_end_time) would silently zero out recent usage. Join keys are cloud + usage_unit + sku_name, not sku_name alone (multi-cloud accounts have several rows per sku_name). currency_code is not a column on system.billing.usage, so it cannot drive the join; multi-currency accounts may match more than one list_prices currency row and that is not disambiguated here. net_usage_quantity sums usage_quantity across all record_types (corrections already net out). usage_unit is filtered to DBU so storage-bytes/hours/tokens are never priced against a per-DBU rate. The price is LEFT-joined: a SKU with no matching price row keeps net_dbus but a null net_list_cost - read that as a priced-coverage gap, never as $0. No price_basis column here (free/priced/unpriced) since every row is NOT_ASSESSED regardless of pricing coverage. workspace_id lets this check obey the workspace/env filter the way every other per-workspace finding already does; a NULL workspace_id is a genuine account-level usage row, never dropped.
SELECT u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product,
       SUM(u.usage_quantity)                       AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate)        AS net_list_cost,
       -- status: this check can never be assessed - no negotiated-rate source exists anywhere in
       -- system.billing, so every row reads NOT_ASSESSED instead of a fabricated realization band.
       'NOT_ASSESSED'                                                      AS status,
       'no_negotiated_rate_source'                                        AS not_assessed_reason
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1's basis
  FROM system.billing.list_prices
) lp
  ON  u.sku_name      = lp.sku_name
  AND u.cloud         = lp.cloud
  AND u.usage_unit    = lp.usage_unit
  AND u.usage_date    >= DATE(lp.price_start_time)
  AND (lp.price_end_time IS NULL OR u.usage_date < DATE(lp.price_end_time))   -- active rows carry NULL end_time
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
  AND upper(u.usage_unit) = 'DBU'   -- price only DBU rows against a per-DBU rate; never blend bytes/hours/tokens
GROUP BY u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product
ORDER BY net_list_cost DESC NULLS LAST
