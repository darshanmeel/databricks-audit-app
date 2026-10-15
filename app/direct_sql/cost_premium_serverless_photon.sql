-- generated from dbt/models/databricks_direct/cost/d_cost_premium_serverless_photon.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_premium_serverless_photon.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product,
       product_features.is_serverless      AS is_serverless,
       product_features.is_photon          AS is_photon,
       product_features.jobs_tier          AS jobs_tier,
       product_features.sql_tier           AS sql_tier,
       product_features.dlt_tier           AS dlt_tier,
       product_features.performance_target AS performance_target,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product,
         product_features.is_serverless, product_features.is_photon, product_features.jobs_tier,
         product_features.sql_tier, product_features.dlt_tier, product_features.performance_target
ORDER BY usage_date DESC, sku_name, cloud
) q
