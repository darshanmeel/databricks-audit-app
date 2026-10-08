-- generated from dbt/models/databricks_direct/cost/d_cost_by_serving_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_serving_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.endpoint_id AS endpoint_id,
       usage_metadata.endpoint_name AS endpoint_name,
       usage_type,
       SUM(usage_quantity) AS net_usage_quantity
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_unit = 'DBU'
  AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH')
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.endpoint_id, usage_metadata.endpoint_name, usage_type
ORDER BY usage_date DESC, workspace_id, endpoint_id
) q
