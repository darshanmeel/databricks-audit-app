-- generated from dbt/models/databricks_direct/compute/d_sql_warehouse_config_current.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/sql_warehouse_config_current.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT warehouse_id, warehouse_name AS warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
       warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time
FROM (
  SELECT warehouse_id, warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
         warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY warehouse_id
) q
