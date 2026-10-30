-- generated from dbt/models/databricks_direct/cost/d_cost_by_notebook.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_notebook.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.notebook_id     AS notebook_id,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: magnitude band on daily DBU cost per notebook (field heuristic; 10 / 50).
       CASE
         WHEN SUM(usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(usage_quantity) >= 50 THEN 'CRITICAL'
         WHEN SUM(usage_quantity) >= 10 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_unit = 'DBU'
  AND usage_metadata.notebook_id IS NOT NULL
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.notebook_id, product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
