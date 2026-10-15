-- query_id: cost_default_storage_dsu
-- title: Default storage (DSU) usage
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Databricks' default storage cost page filters billing_origin_product = 'DEFAULT_STORAGE'; storage_api_type is filled only on API-operation rows, so both are matched.
-- read_this: One row = a workspace + day + cloud + SKU + storage-API-type + catalog's default-storage usage. The columns that matter are usage_type (STORAGE_SPACE for stored data), storage_api_type (TIER_1 vs TIER_2 API operations, NULL on stored-data rows) and net_usage_quantity - this is Unity Catalog default/managed storage cost, not a customer-managed external location.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the total spend context), po_vacuum_reclaimed_bytes (for storage reclaimed by VACUUM in the same window)
-- caveats: The literal billing_origin_product = 'DEFAULT_STORAGE' is unverified, so this query filters on the confirmed signal usage_metadata. Rows are matched on billing_origin_product = 'DEFAULT_STORAGE' or a storage_api_type, so stored data and API operations both count. Treat the most recent day's usage_quantity as provisional because of billing populate lag. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it does not change any other column's meaning.
SELECT usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
       usage_metadata.storage_api_type AS storage_api_type,
       usage_metadata.catalog_id       AS catalog_id,
       SUM(usage_quantity) AS net_usage_quantity
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  -- the product covers stored data (STORAGE_SPACE); storage_api_type is set on API-operation rows only
  AND (billing_origin_product = 'DEFAULT_STORAGE' OR usage_metadata.storage_api_type IS NOT NULL)
GROUP BY usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
         usage_metadata.storage_api_type, usage_metadata.catalog_id
ORDER BY usage_date DESC, cloud, sku_name
