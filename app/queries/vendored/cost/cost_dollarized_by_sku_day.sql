-- query_id: cost_dollarized_by_sku_day
-- title: Dollarized cost by SKU and day (list price)
-- domain: cost   tier: deep
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices are generally available)
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: pricing.effective_list.default (the effective, post-promotion list price) is the one dollar basis used across this app (DEC-66.1); net_list_cost is a directional estimate at that price, never a negotiated or billed dollar.
-- read_this: One row = a day + workspace + cloud + SKU + product + usage type/unit's usage, priced at the effective list price. The columns that matter are net_usage_quantity (the native-unit volume) and net_list_cost (usage_quantity x the effective list rate) - this is a pre-discount estimate, not what you actually pay. price_basis (free/priced/unpriced) discloses whether net_list_cost is a real $0 (free-usage SKU) or understated by a pricing-coverage gap.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: pricing_list_prices_raw (for the raw price rows behind net_list_cost), cost_actual_vs_list_by_sku (for why a discount-realization comparison cannot be assessed on this basis)
-- caveats: This is the effective list price (DEC-66.1: pricing.effective_list.default, post-promotion) - not a negotiated rate, so apply any negotiated discount factor yourself; net_list_cost is dollarized so it is safe to sum even though the underlying usage spans several usage_type/usage_unit families. The price window is open-interval: price_end_time NULL means the currently effective price. The price join is keyed on sku_name + cloud + usage_unit (matching cost_actual_vs_list_by_sku/cost_cloud_infra's own join -- a multi-cloud or multi-unit account can carry more than one price row per sku_name+cloud alone). price_basis is 'unpriced' when this SKU/day/cloud row had no matching list_prices row and is not a FREE_USAGE SKU (net_list_cost is then NULL, a coverage gap, never $0), 'free' when the SKU is a FREE_USAGE SKU with no price row by design (a real $0), and 'priced' otherwise. workspace_id lets the Cost tab's own $ tiles obey the workspace/env filter the way every other per-workspace finding already does; a NULL workspace_id is a genuine account-level usage row, never dropped.
SELECT u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
       lp.currency_code,
       SUM(u.usage_quantity)               AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate) AS net_list_cost,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
       CASE
         WHEN lp.list_rate IS NOT NULL THEN 'priced'
         WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
         ELSE 'unpriced'
       END                                  AS price_basis
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM system.billing.list_prices
) lp
  ON u.sku_name   = lp.sku_name
 AND u.cloud      = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
GROUP BY u.workspace_id, u.usage_date, u.cloud, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit, lp.currency_code,
         CASE
           WHEN lp.list_rate IS NOT NULL THEN 'priced'
           WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
           ELSE 'unpriced'
         END
ORDER BY u.usage_date DESC, u.cloud, u.sku_name
