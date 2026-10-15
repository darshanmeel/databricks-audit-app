-- query_id: overview_daily_dbu_trend
-- title: Daily net-DBU trend by workspace
-- domain: cost   tier: lite
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Ported from the reference overview set, not yet run live; confirm
--   SUM(usage_quantity) across all record_types matches the usage dashboard total for the same
--   window.
-- read_this: One row = a workspace + day's total net DBU usage. Use this as the detail behind
--   an daily spend trend sparkline or chart.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: overview_spend_estimate (for the total over the window), cost_dollarized_by_sku_day
--   (for the same detail by day, SKU, and product)
-- caveats: Net DBUs SUM across ALL record_types (never filter to ORIGINAL only). DBU-only
--   (usage_unit='DBU'). The current day is excluded. Ported from the MIT-licensed reference
--   (c) 2026 darshanmeel.
SELECT
    workspace_id,
    usage_date,
    SUM(usage_quantity) AS net_dbus
FROM system.billing.usage
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY workspace_id, usage_date
ORDER BY usage_date
