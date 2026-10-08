-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_phase_cold_start.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_phase_cold_start.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH run_level AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT run_id)                                                      AS runs,
         SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)     AS queue_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN execution_duration_seconds END) AS execution_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN cleanup_duration_seconds END)   AS cleanup_s_total,
         SUM(run_duration_seconds)                                                   AS run_s_total,
         PERCENTILE(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    AND result_state IS NOT NULL     -- end row carries the final durations
  GROUP BY workspace_id, job_id
),
task_setup AS (
  SELECT workspace_id, job_id,
         COUNT(*)                                              AS task_rows,
         SUM(setup_duration_seconds)                           AS setup_s_total,
         PERCENTILE(setup_duration_seconds, 0.95)               AS setup_s_p95,
         SUM(CASE WHEN setup_duration_seconds IS NULL THEN 1 ELSE 0 END) AS rows_setup_null
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    -- task end row carries the final durations; a failed start's long setup is not a cold start
    AND result_state = 'SUCCEEDED'
  GROUP BY workspace_id, job_id
)
SELECT r.workspace_id, r.job_id,
       r.runs,
       ts.setup_s_total,
       r.queue_s_total,
       r.execution_s_total,
       r.cleanup_s_total,
       r.run_s_total,
       ts.setup_s_p95,
       r.queue_s_p95,
       COALESCE(ts.rows_setup_null, r.runs)                    AS rows_setup_null,
       -- status: worst-first band on p95 setup (cold-start) seconds (field heuristic;
       -- 60 / 300), from task_setup so a multi-task job is judged
       -- too. A job with no task row in the window has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed task's setup folding to NULL (also NOT_ASSESSED, via the
       -- p95-is-NULL branch below).
       CASE
         WHEN ts.task_rows IS NULL OR ts.task_rows = 0 THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 IS NULL THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 >= 300 THEN 'CRITICAL'
         WHEN ts.setup_s_p95 >= 60 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM run_level r
LEFT JOIN task_setup ts ON r.workspace_id = ts.workspace_id AND r.job_id = ts.job_id
ORDER BY setup_s_p95 DESC
) q
