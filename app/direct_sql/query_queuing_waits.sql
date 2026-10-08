-- generated from dbt/models/databricks_direct/performance/d_query_queuing_waits.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_queuing_waits.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH per_query AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         waiting_at_capacity_duration_ms, waiting_for_compute_duration_ms, total_duration_ms,
         COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0) AS wait_ms
  FROM `system`.`query`.`history`
  WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND start_time < __AS_OF_DATE__
)
SELECT day, workspace_id, compute_type, warehouse_id,
       COUNT(*) AS query_count,
       SUM(CASE WHEN waiting_at_capacity_duration_ms > 0 THEN 1 ELSE 0 END) AS queued_at_capacity_count,
       SUM(CASE WHEN waiting_for_compute_duration_ms > 0 THEN 1 ELSE 0 END) AS waited_for_compute_count,
       SUM(waiting_at_capacity_duration_ms)  AS waiting_at_capacity_ms_sum,
       SUM(waiting_for_compute_duration_ms)  AS waiting_for_compute_ms_sum,
       SUM(total_duration_ms)                AS total_duration_ms_sum,
       -- avg_wait_ms: the average combined wait of statements that waited at all (a real, all-zero
       -- day has no such statement -> NULL, never 0 and never NOT_ASSESSED on its own).
       SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
         / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) AS avg_wait_ms,
       -- status: worst-first band on the PER-STATEMENT average wait (field heuristic;
       -- 60 / 600), not the day's raw total - so many tiny waits on a
       -- busy warehouse no longer outrank one genuinely long wait. NOT_ASSESSED is checked only
       -- when avg_wait_ms does not already clear a band, and triggers when EITHER bucket's raw SUM
       -- is NULL across the whole group (e.g. a SERVERLESS_COMPUTE day that never reports
       -- waiting_at_capacity at all) - the same rule as before, unaffected by the average.
       CASE
         WHEN SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) >= 600 * 1000 THEN 'CRITICAL'
         WHEN SUM(CASE WHEN wait_ms > 0 THEN wait_ms ELSE 0 END)
                / NULLIF(SUM(CASE WHEN wait_ms > 0 THEN 1 ELSE 0 END), 0) >= 60 * 1000 THEN 'WARN'
         WHEN SUM(waiting_at_capacity_duration_ms) IS NULL OR SUM(waiting_for_compute_duration_ms) IS NULL THEN 'NOT_ASSESSED'
         ELSE 'OK'
       END AS status
FROM per_query
GROUP BY day, workspace_id, compute_type, warehouse_id
ORDER BY (waiting_at_capacity_ms_sum + waiting_for_compute_ms_sum) DESC
) q
