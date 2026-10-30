-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_tasks_near_timeout.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_tasks_near_timeout.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time
  FROM `system`.`lakeflow`.`job_tasks`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
task_end AS (
  SELECT workspace_id, job_id, task_key, run_id, execution_duration_seconds
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- task end row only
),
agg AS (
  SELECT t.workspace_id, t.job_id, t.task_key,
         lt.timeout_seconds,
         COUNT(DISTINCT t.run_id)                              AS task_runs,
         MAX(t.execution_duration_seconds)                     AS max_exec_s,
         percentile(t.execution_duration_seconds, 0.95) AS exec_s_p95,
         -- runs whose execution reached >= 0.8 of a CONFIGURED (>0) timeout but
         -- stayed under it -- mutually exclusive with runs_over_timeout, so the two never overlap.
         SUM(CASE WHEN lt.timeout_seconds > 0
                   AND t.execution_duration_seconds >= 0.8 * lt.timeout_seconds
                   AND t.execution_duration_seconds <  lt.timeout_seconds
                  THEN 1 ELSE 0 END) AS runs_near_timeout,
         -- runs that ran AT/PAST the configured timeout (should have been killed)
         SUM(CASE WHEN lt.timeout_seconds > 0
                   AND t.execution_duration_seconds >= lt.timeout_seconds
                  THEN 1 ELSE 0 END) AS runs_over_timeout,
         -- an explicit 0-second timeout is "no bound configured" (a deliberate setting, not a gap).
         SUM(CASE WHEN lt.timeout_seconds = 0 THEN 1 ELSE 0 END) AS runs_no_timeout_bound,
         -- degradation buckets: a NULL timeout / NULL execution duration is "column not yet
         -- populated" (not assessed), distinct from an explicit 0-second timeout above.
         SUM(CASE WHEN lt.timeout_seconds IS NULL THEN 1 ELSE 0 END) AS runs_no_task_timeout,
         SUM(CASE WHEN t.execution_duration_seconds IS NULL THEN 1 ELSE 0 END) AS runs_exec_null
  FROM task_end t
  LEFT JOIN latest_tasks lt
    ON  t.workspace_id = lt.workspace_id
    AND t.job_id       = lt.job_id
    AND t.task_key     = lt.task_key
  GROUP BY t.workspace_id, t.job_id, t.task_key, lt.timeout_seconds
)
SELECT a.workspace_id, a.job_id, j.job_name, w.workspace_name, a.task_key, a.timeout_seconds,
       a.task_runs, a.max_exec_s, a.exec_s_p95,
       a.runs_near_timeout, a.runs_over_timeout, a.runs_no_timeout_bound,
       a.runs_no_task_timeout, a.runs_exec_null,
       -- status: worst-first band on near/over-timeout run count (field heuristic;
       -- 3 / 10), degraded to NOT_ASSESSED when either
       -- NULL bucket accounts for a material share of the task's runs.
       CASE
         WHEN a.runs_no_task_timeout >= 0.5 * a.task_runs THEN 'NOT_ASSESSED'
         WHEN a.runs_exec_null       >= 0.5 * a.task_runs THEN 'NOT_ASSESSED'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= 10 THEN 'CRITICAL'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.runs_no_task_timeout >= 0.5 * a.task_runs THEN 'timeout_not_populated'
         WHEN a.runs_exec_null       >= 0.5 * a.task_runs THEN 'exec_duration_not_populated'
       END AS not_assessed_reason
FROM agg a
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = a.workspace_id
ORDER BY (a.runs_near_timeout + a.runs_over_timeout) DESC
) q
