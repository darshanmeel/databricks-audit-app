-- query_id: overview_dbu_by_sku
-- title: Where the DBUs go - net DBUs by workspace, SKU and product
-- domain: cost   tier: lite
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Ported from the reference overview set, not yet run live; confirm
--   SUM(usage_quantity) across all record_types matches the usage dashboard total for the same
--   window.
-- read_this: One row = a workspace + SKU + billing product's net DBU usage over the window ...
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the same cut by day), cost_premium_serverless_photon (if
--   serverless or Photon SKUs dominate), cost_by_job (if jobs SKUs dominate)
-- caveats: net DBUs SUM across ALL record_types ...; usage_quantity is DBU, not dollars ...;
--   DBU-only ...; the current day is excluded ...; ported from the MIT-licensed reference
--   (c) 2026 darshanmeel.
SELECT
    workspace_id,
    sku_name,
    billing_origin_product,
    SUM(usage_quantity) AS net_dbus
FROM system.billing.usage
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY workspace_id, sku_name, billing_origin_product
ORDER BY net_dbus DESC
