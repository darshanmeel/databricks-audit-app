-- generated from dbt/models/databricks_direct/performance/d_query_per_query_estimate_lane.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_per_query_estimate_lane.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date_trunc('HOUR', start_time) AS usage_hour, workspace_id,
       compute.warehouse_id AS warehouse_id, compute.type AS compute_type,
       executed_by AS executed_by,
       statement_id, statement_type,
       execution_duration_ms, waiting_for_compute_duration_ms, total_task_duration_ms,
       read_bytes, total_duration_ms
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
  AND execution_status = 'FINISHED'
  AND from_result_cache = false
  AND execution_duration_ms > 0
  AND compute.warehouse_id IS NOT NULL
ORDER BY usage_hour, warehouse_id, statement_id
) q
