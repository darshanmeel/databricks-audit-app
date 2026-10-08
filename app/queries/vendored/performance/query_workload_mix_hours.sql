-- query_id: query_workload_mix_hours
-- title: Workload mix by hour of day
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, compute_scope_gap, privilege_scoped
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + hour-of-day + compute + statement_type + identity slice of your query workload. The columns that matter are query_count and total_duration_ms_sum - stack these by hour to see when your workload actually peaks, and whether that peak lines up with warehouse auto-scaling or scheduled auto-stop.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: query_queuing_waits (if a peak hour also shows queuing), compute_warehouse_autoscale_churn (to check whether autoscaling tracks this hourly pattern)
-- caveats: This is a day x hour-of-day x compute x statement_type x identity histogram - group further at your own risk, the cardinality is already high. produced_rows (rows returned to the caller) is a different thing from read_rows (rows scanned) - do not conflate them. Only SQL-warehouse and serverless compute are covered; classic all-purpose clusters are not. This table is regional. The current, still-in-progress day is excluded, so today will always look artificially quiet.
SELECT date(start_time) AS day, workspace_id, hour(start_time) AS hour_of_day,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       statement_type,
       CASE
         WHEN executed_by IS NULL OR executed_by = '__REDACTED__' THEN executed_by
         WHEN executed_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN executed_by
         ELSE concat(COALESCE(MAX(executed_by_user_id), substr(sha2(lower(trim(executed_by)), 256), 1, 8)), ' ', substr(executed_by, 1, 2), '***')
       END AS executed_by,
       COUNT(*) AS query_count,
       SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END) AS finished_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum,
       SUM(produced_rows)         AS produced_rows_sum
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
GROUP BY date(start_time), workspace_id, hour(start_time), compute.type, compute.warehouse_id, statement_type, executed_by
ORDER BY day, hour_of_day, workspace_id, compute_type, warehouse_id
