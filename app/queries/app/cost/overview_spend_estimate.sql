-- query_id: overview_spend_estimate
-- title: Estimated net spend - net DBUs x list price by workspace
-- domain: cost   tier: lite
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Ported from the reference overview set, not yet run live; confirm
--   SUM(usage_quantity) across all record_types matches the usage dashboard total for the same
--   window. Dollarizes at pricing.effective_list.default, DEC-66.1's one effective-list-price
--   basis used everywhere in this app (no negotiated-rate source exists; see
--   cost_actual_vs_list_by_sku for why an "actual" figure can never be produced).
-- read_this: One row = a workspace's total net DBU usage and list-price dollar estimate over
--   the window (currency_code is always USD, the only currency the shared price step keeps).
--   price_basis (free/priced/unpriced) discloses whether net_list_cost_usd mixes free-usage SKUs
--   (a real $0) or is understated by a pricing-coverage gap.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: overview_list_prices_raw (for the safe raw JSON pricing basis), cost_dollarized_by_sku_day
--   (for the same cut by day and product)
-- caveats: The effective list price (DEC-66.1) only - never a negotiated rate, DBU-only, excludes
--   cloud infra/egress. NEVER present net_list_cost_usd as a billed dollar; it is a list estimate,
--   never negotiated (no negotiated-rate source exists in system.billing). If pricing.effective_list.default
--   ever errors or looks wrong on your workspace, dollarize in-engine off the raw JSON via
--   overview_list_prices_raw.csv. Net DBUs SUM across ALL record_types (never filter to ORIGINAL
--   only). usage_quantity is DBU, not dollars. The current day is excluded. price_basis is
--   'unpriced' when any non-free-usage SKU in this workspace had no matching list_prices row
--   (net_list_cost_usd then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU
--   (a real $0), and 'priced' otherwise. Ported from the
--   MIT-licensed reference (c) 2026 darshanmeel.
SELECT
    workspace_id,
    SUM(u.usage_quantity) AS total_net_dbus,
    'USD' AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2) AS net_list_cost_usd,
    'list' AS dbu_price_source,
    -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
    -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) lp
  ON u.sku_name = lp.sku_name
 AND u.cloud = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_unit = 'DBU'
  AND u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
GROUP BY workspace_id
