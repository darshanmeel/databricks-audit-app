-- generated from dbt/models/databricks_direct/performance/d_query_local_spillage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_local_spillage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       executed_by AS executed_by,
       COUNT(*) AS spilling_query_count,
       SUM(spilled_local_bytes) AS spilled_local_bytes_sum,
       MAX(spilled_local_bytes) AS spilled_local_bytes_max,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(shuffle_read_bytes)    AS shuffle_read_bytes_sum,
       -- status: worst-first band on daily local spill (field heuristic; 1 / 10).
       CASE
         WHEN SUM(spilled_local_bytes) >= 10 * 1e9 THEN 'CRITICAL'
         WHEN SUM(spilled_local_bytes) >= 1 * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
  AND spilled_local_bytes > 0
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by
ORDER BY spilled_local_bytes_sum DESC
) q
