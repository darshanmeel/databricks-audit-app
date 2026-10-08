-- generated from dbt/models/databricks_direct/performance/d_perf_queue_by_hour.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/perf_queue_by_hour.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT date(start_time) AS usage_date,
       hour(start_time) AS usage_hour,
       concat(workspace_id, ':', compute.warehouse_id) AS warehouse_key,
       workspace_id,
       compute.warehouse_id AS warehouse_id,
       COUNT(*) AS runs,
       SUM(CASE WHEN COALESCE(waiting_at_capacity_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS queued_runs,
       ROUND(SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0, 1) AS slot_wait_s,
       SUM(CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS provision_runs,
       ROUND(SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0, 1) AS provision_s
FROM `system`.`query`.`history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
  AND compute.warehouse_id IS NOT NULL
GROUP BY date(start_time), hour(start_time), workspace_id, compute.warehouse_id
ORDER BY usage_date DESC, usage_hour, warehouse_key
) q
