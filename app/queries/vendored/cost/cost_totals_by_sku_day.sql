-- query_id: cost_totals_by_sku_day
-- title: Cost totals by SKU and day
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: The record_type enum and product_features.is_serverless are documented system.billing.usage columns.
-- read_this: One row = a day + cloud + workspace + SKU + product + usage type's net usage. The columns that matter are net_usage_quantity (the corrected net) and workspace_id (NULL means an account-level SKU, not a missing value) - this is the finest-grain total this repo collects, and most other cost cuts are a slice of it.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_by_billing_origin_product (for the same window rolled up to product line only), cost_workspace_names (to resolve workspace_id to a human name)
-- caveats: Corrections are netted by summing usage_quantity across all record_types - never filter to ORIGINAL only. usage_quantity is DBU/units, not dollars. usage_date is the recommended date-partition column. workspace_id is NULL for account-level SKUs (some MODEL_SERVING/account services) - keep those NULL rows, they roll up as "account-level, not workspace-attributable." SKU/product totals are unaffected by the finer workspace grain: sum across it to get the coarser total. Workspace names are not in billing.usage - resolve workspace_id to a name via cost_workspace_names or the account console/API.
-- billing.usage lands with ingestion lag, so the most recent day or two in the window can be under-counted and appear artificially low until usage fully materializes - treat the trailing edge as provisional.
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN usage_quantity ELSE 0 END) AS retraction_usage_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity ELSE 0 END) AS restatement_usage_quantity,
       COUNT(*) AS record_count
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
         product_features.is_serverless
ORDER BY usage_date DESC, workspace_id, sku_name
