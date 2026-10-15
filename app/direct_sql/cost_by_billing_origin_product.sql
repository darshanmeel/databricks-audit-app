-- generated from dbt/models/databricks_direct/cost/d_cost_by_billing_origin_product.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_billing_origin_product.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT billing_origin_product, usage_unit, cloud, workspace_id,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*)            AS record_count
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
GROUP BY billing_origin_product, usage_unit, cloud, workspace_id
ORDER BY billing_origin_product, usage_unit, cloud, workspace_id
) q
