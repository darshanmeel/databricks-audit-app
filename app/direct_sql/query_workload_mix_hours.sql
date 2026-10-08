-- generated from dbt/models/databricks_direct/performance/d_query_workload_mix_hours.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, hour(start_time) AS hour_of_day,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       statement_type,
       executed_by AS executed_by,
       COUNT(*) AS query_count,
       SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END) AS finished_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum,
       SUM(produced_rows)         AS produced_rows_sum
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
GROUP BY date(start_time), workspace_id, hour(start_time), compute.type, compute.warehouse_id, statement_type, executed_by
ORDER BY day, hour_of_day, workspace_id, compute_type, warehouse_id
) q
