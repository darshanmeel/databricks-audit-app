-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_never_started_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_never_started_runs.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH runs AS (
  SELECT workspace_id, job_id, run_id, termination_code, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
    AND result_state IS NOT NULL
),
task_rows AS (
  SELECT DISTINCT workspace_id, job_id, job_run_id AS run_id
  FROM `system`.`lakeflow`.`job_task_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
),
flagged AS (
  SELECT r.workspace_id, r.job_id, r.run_id, r.termination_code,
         (r.period_start_time = r.period_end_time)                               AS is_zero_length,
         (r.termination_code = 'INVALID_RUN_CONFIGURATION' AND t.run_id IS NULL) AS is_invalid_config_no_tasks
  FROM runs r
  LEFT JOIN task_rows t
    ON  t.workspace_id = r.workspace_id
    AND t.job_id       = r.job_id
    AND t.run_id       = r.run_id
),
grouped AS (
  SELECT workspace_id, job_id, termination_code,
         COUNT(DISTINCT CASE WHEN is_zero_length THEN run_id END)                                AS never_started_runs_zero_length,
         COUNT(DISTINCT CASE WHEN is_invalid_config_no_tasks THEN run_id END)                    AS never_started_runs_invalid_config_no_tasks,
         COUNT(DISTINCT CASE WHEN is_zero_length OR is_invalid_config_no_tasks THEN run_id END) AS never_started_runs
  FROM flagged
  GROUP BY workspace_id, job_id, termination_code
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
)
SELECT g.workspace_id, g.job_id, j.job_name, w.workspace_name, g.termination_code,
       g.never_started_runs_zero_length, g.never_started_runs_invalid_config_no_tasks,
       g.never_started_runs,
       -- status: worst-first band on never-started run count (field heuristic; 3 / 10).
       CASE
         WHEN g.never_started_runs >= 10 THEN 'CRITICAL'
         WHEN g.never_started_runs >= 3 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM grouped g
LEFT JOIN latest_jobs j ON j.workspace_id = g.workspace_id AND j.job_id = g.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = g.workspace_id
ORDER BY never_started_runs DESC
) q
