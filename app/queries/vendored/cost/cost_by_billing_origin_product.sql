-- query_id: cost_by_billing_origin_product
-- title: Usage by product line
-- domain: cost   tier: lite
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: billing_origin_product and usage_unit are documented system.billing.usage columns.
-- read_this: One row = a workspace's product line + usage unit + cloud total usage for the window. The columns that matter are billing_origin_product (where the DBUs/bytes/tokens went - JOBS, SQL, MODEL_SERVING, DLT, and so on) and net_usage_quantity (the summed volume in that row's native unit). A NULL workspace_id is a genuine account-level usage row (some networking/storage SKUs bill with no workspace_id at all), kept as its own line.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the same window broken out by SKU and workspace), cost_by_job (to drill a JOBS spike down to individual jobs)
-- caveats: net_usage_quantity sums usage_quantity across all record_types, so billing corrections (ORIGINAL/RETRACTION/RESTATEMENT) already net out - do not re-net this again and do not filter to record_type='ORIGINAL' for a net rollup. usage_unit is carried so you can isolate DBU rows; non-DBU families (STORAGE_SPACE bytes, hours, TOKEN) are on their own scale and must never be summed into DBUs. billing_origin_product is the product line; it is populated for billed usage, but a blank/NULL value means "unattributed product," not zero. There are no dollars here - usage_quantity is DBUs/units.
SELECT billing_origin_product, usage_unit, cloud, workspace_id,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*)            AS record_count
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY billing_origin_product, usage_unit, cloud, workspace_id
ORDER BY billing_origin_product, usage_unit, cloud, workspace_id
