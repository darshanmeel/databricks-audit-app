-- query_id: cost_vector_search_spend
-- title: Vector Search spend by endpoint
-- domain: cost   tier: deep
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices are generally available)
-- empty_if: no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_endpoint_usd_per_day (default 25) estimated list-price $/day on a single Vector Search endpoint that flags WARN; :crit_endpoint_usd_per_day (default 100) $/day that flags CRITICAL
-- confidence: confirmed
-- confidence_note: billing_origin_product='VECTOR_SEARCH', usage_metadata.endpoint_name, and the usage_type enum are confirmed in docs; pricing.effective_list.default (the effective, post-promotion list price - DEC-66.1) is the one dollar basis used across this app, so net_list_cost is a directional estimate at that price.
-- read_this: One row = a workspace + Vector Search endpoint + day + usage type's spend. The columns that matter are usage_type (STORAGE_SPACE is DSU storage, everything else is serving DBUs - different units) and net_list_cost (est_usd_list, the dollarized driver behind the band, which is safe to compare across usage_type because it is already in dollars). price_basis (free/priced/unpriced) discloses whether net_list_cost is a real $0 (free-usage SKU) or understated by a pricing-coverage gap.
-- healthy: net_list_cost below :warn_endpoint_usd_per_day est_usd_list/day per endpoint (field heuristic - tune for your account).
-- investigate_if: net_list_cost at/above :warn_endpoint_usd_per_day (WARN) or :crit_endpoint_usd_per_day (CRITICAL) est_usd_list/day (field heuristic); or net_list_cost is NULL on a row whose price_basis is unpriced (NOT_ASSESSED - a pricing-coverage gap, not $0).
-- actions: 1) confirm the endpoint's index is still queried by something (free) - cross-check against real traffic before assuming it is idle; 2) shrink or consolidate an oversized/underused index, or right-size its DSU tier (config); 3) if traffic genuinely justifies the spend, budget for it deliberately (spend).
-- next: cost_by_serving_endpoint (for the raw, non-dollarized MODEL_SERVING + VECTOR_SEARCH usage), compute_serving_endpoint_cost_status (to check whether a CRITICAL endpoint is actually receiving traffic before you shrink it)
-- caveats: billing_origin_product='VECTOR_SEARCH', usage_metadata.endpoint_name (the Vector Search endpoint), and the usage_type enum (STORAGE_SPACE = DSU storage, everything else = serving DBUs) are confirmed in the billing and vector-search cost-management documentation. STORAGE_SPACE rows are DSU-denominated and serving rows are DBU-denominated - different units, never summed directly; net_list_cost dollarizes both onto the same $ scale, which is why it (not net_usage_quantity) drives the band. net_list_cost uses list_prices.pricing.effective_list.default (DEC-66.1's one basis, same path as cost_dollarized_by_sku_day) - this is a list/pre-discount estimate (est_usd_list); apply your own discount factor and never promote it to a billed headline. The price window is open-interval: price_end_time NULL means the currently effective price. The price join is keyed on sku_name + cloud + usage_unit, same as cost_dollarized_by_sku_day, so a SKU priced in two units cannot fan out. price_basis is 'unpriced' when this endpoint/day/usage_type/SKU row had no matching list_prices row and is not a FREE_USAGE SKU (net_list_cost is then NULL, a coverage gap, never $0), 'free' when the SKU is a FREE_USAGE SKU with no price row by design (a real $0), and 'priced' otherwise. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it never changes the endpoint + day + usage_type grain, since one endpoint bills to one workspace.
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.endpoint_name AS endpoint_name,
       u.usage_metadata.endpoint_id   AS endpoint_id,
       lp.currency_code,
       SUM(u.usage_quantity)                AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
       CASE
         WHEN lp.list_rate IS NOT NULL THEN 'priced'
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
         ELSE 'unpriced'
       END                                   AS price_basis,
       -- status: est_usd_list/day band per endpoint (field heuristic; :warn_endpoint_usd_per_day / :crit_endpoint_usd_per_day).
       -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
       -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
       CASE
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
         WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity * lp.list_rate) >= :crit_endpoint_usd_per_day THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity * lp.list_rate) >= :warn_endpoint_usd_per_day THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, currency_code, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM system.billing.list_prices
) lp
  ON u.sku_name = lp.sku_name
 AND u.cloud    = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.billing_origin_product = 'VECTOR_SEARCH'
  AND u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, lp.currency_code,
         CASE
           WHEN lp.list_rate IS NOT NULL THEN 'priced'
           WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
           ELSE 'unpriced'
         END
ORDER BY net_list_cost DESC NULLS LAST
