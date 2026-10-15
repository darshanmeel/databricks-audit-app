-- generated from dbt/models/databricks_direct/cost/d_cost_totals_by_sku_day.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_totals_by_sku_day.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN usage_quantity ELSE 0 END) AS retraction_usage_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity ELSE 0 END) AS restatement_usage_quantity,
       COUNT(*) AS record_count
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
         product_features.is_serverless
ORDER BY usage_date DESC, workspace_id, sku_name
) q
