-- generated from dbt/models/databricks_direct/cost/d_cost_default_storage_dsu.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_default_storage_dsu.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
       usage_metadata.storage_api_type AS storage_api_type,
       usage_metadata.catalog_id       AS catalog_id,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_metadata.storage_api_type IS NOT NULL   -- confirmed default-storage signal (safer fallback)
GROUP BY usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
         usage_metadata.storage_api_type, usage_metadata.catalog_id
ORDER BY usage_date DESC, cloud, sku_name
) q
