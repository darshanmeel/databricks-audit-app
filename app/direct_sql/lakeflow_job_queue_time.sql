-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_queue_time.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_job_queue_time.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, queue_duration_seconds, run_duration_seconds
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
)
SELECT workspace_id, job_id,
       COUNT(DISTINCT run_id)                                   AS distinct_runs,
       SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)                   AS queue_s_total,
       percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95,
       SUM(CASE WHEN run_duration_seconds > 0 AND queue_duration_seconds IS NOT NULL THEN 0 ELSE 1 END) AS runs_queue_null,
       -- status: worst-first band on p95 queue seconds (field heuristic; 60 / 300).
       -- A job with no run reporting real wall-clock time has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed run's queue phase folding to NULL (also NOT_ASSESSED, via
       -- the p95-is-NULL branch below). Reading queue_duration_seconds only where
       -- run_duration_seconds > 0 keeps a genuine 0-second queue on a legacy single-task job as a
       -- real 0 (OK), instead of NULLIF(queue_duration_seconds, 0) folding EVERY 0 to NULL and
       -- inflating p95 off queued runs alone.
       CASE
         WHEN SUM(CASE WHEN run_duration_seconds > 0 THEN 1 ELSE 0 END) = 0 THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) IS NULL THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= 300 THEN 'CRITICAL'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= 60 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows
GROUP BY workspace_id, job_id
ORDER BY queue_s_p95 DESC
) q
