-- hand-written; not generated. Databricks twin of dbt/models/tags/perf_unit.sql: one row per
-- (window_days, unit_id), grouped from system.query.history by (workspace_id, compute_kind,
-- compute_id) over the window (by start_time, the vendored query.history convention).
-- __WINDOW_DAYS__ is filled in at export time.
WITH history AS (
    SELECT __WINDOW_DAYS__ AS window_days, q.workspace_id,
           CASE WHEN q.compute.warehouse_id IS NOT NULL THEN 'warehouse'
                WHEN q.compute.cluster_id IS NOT NULL THEN 'cluster'
                ELSE 'serverless' END AS compute_kind,
           COALESCE(q.compute.warehouse_id, q.compute.cluster_id) AS compute_id,
           q.execution_status, q.total_duration_ms,
           COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0) AS queue_ms,
           q.spilled_local_bytes
    FROM `system`.`query`.`history` q
    WHERE q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND q.start_time < __AS_OF_DATE__
),
hashed AS (
    SELECT *,
           md5(COALESCE(workspace_id, '~') || '|' || compute_kind || '|' || COALESCE(compute_id, '~')) AS unit_id
    FROM history
)
SELECT
    window_days,
    unit_id,
    ANY_VALUE(workspace_id) AS workspace_id,
    ANY_VALUE(compute_kind) AS compute_kind,
    ANY_VALUE(compute_id) AS compute_id,
    COUNT(*) AS statements,
    SUM(CASE WHEN execution_status = 'FAILED' THEN 1 ELSE 0 END) AS failed_statements,
    COALESCE(SUM(total_duration_ms), 0) AS duration_ms,
    COALESCE(SUM(queue_ms), 0) AS queue_ms,
    COALESCE(SUM(spilled_local_bytes), 0) AS spill_bytes
FROM hashed
GROUP BY window_days, unit_id
