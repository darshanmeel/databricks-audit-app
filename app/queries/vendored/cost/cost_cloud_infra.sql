-- query_id: cost_cloud_infra
-- title: Estimated list-price cost by cloud (DBU-derived)
-- domain: cost   tier: deep
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices are generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: system.billing.cloud_infra_cost does not exist in this workspace; this query substitutes a DBU-derived estimate at the effective list price (DEC-66.1), so the dollar figures here are an estimate, not a reconciled cloud bill, and they exclude non-DBU cloud infra/instance/egress cost entirely.
-- read_this: One row = a workspace + day + cloud + currency's estimated cost at the effective list price. The column that matters is net_list_cost, an estimate = usage_quantity x list_prices.pricing.effective_list.default matched to the price row that was active on that usage row - not a substitute for your cloud provider's own cost export. price_basis (free/priced/unpriced) discloses whether that day's total mixes free-usage SKUs or a pricing-coverage gap.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the same window broken out by SKU/workspace instead of cloud/currency), cost_networking_egress (for the DBU-billed egress slice specifically)
-- caveats: system.billing.cloud_infra_cost does not exist in this workspace - system.billing contains only usage, list_prices, and attributed_usage. Cloud-provider infra/instance/egress cost outside DBUs is not available as a system table here and must be treated as not_assessed, never fabricated. As a real substitute, this query estimates dollar cost as usage_quantity x list_prices.pricing.effective_list.default (DEC-66.1's one basis - the effective, post-promotion list price; no negotiated-rate source exists, see cost_actual_vs_list_by_sku), matched on sku_name + cloud + usage_unit within the price's effective window (price_end_time IS NULL = currently effective). currency_code comes from list_prices because system.billing.usage has no currency column. This aggregates by workspace_id / usage_date / cloud / currency_code (workspace_id comes from system.billing.usage, the only side of the join that carries it - list_prices has no workspace column) and deliberately does not join the compute.warehouses / clusters change-history tables, which would fan out rows and double-count the SUM. An empty result means "not assessed," never $0. price_basis is 'unpriced' when any non-free-usage SKU in that day/cloud/currency group had no matching price row (the total then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0, not a gap), and 'priced' otherwise.
SELECT u.workspace_id, u.usage_date, u.cloud, p.currency_code,
       -- bare SUM (no COALESCE): a fully-unpriced day/cloud/currency group must stay NULL, never a
       -- silent $0 -- see caveats ("An empty result means not assessed, never $0").
       SUM(u.usage_quantity * p.list_rate)               AS net_list_cost,
       COUNT(*)                                         AS record_count,
       -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
       -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END                                               AS price_basis
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, currency_code, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) p
  ON u.sku_name = p.sku_name
 AND u.cloud = p.cloud
 AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
GROUP BY u.workspace_id, u.usage_date, u.cloud, p.currency_code
ORDER BY u.usage_date DESC, u.cloud, p.currency_code
