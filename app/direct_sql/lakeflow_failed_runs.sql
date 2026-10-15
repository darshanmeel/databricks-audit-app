-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_failed_runs.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_failed_runs.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, run_type, trigger_type,
         result_state, termination_code, period_start_time, period_end_time
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
grouped AS (
  SELECT workspace_id, job_id, run_type, trigger_type, result_state, termination_code,
         COUNT(*)                AS run_rows,
         COUNT(DISTINCT run_id)  AS distinct_runs,
         SUM(CASE WHEN result_state IN ('FAILED','ERROR','TIMED_OUT') THEN 1 ELSE 0 END) AS failed_run_rows
  FROM end_rows
  GROUP BY workspace_id, job_id, run_type, trigger_type, result_state, termination_code
)
SELECT g.workspace_id, g.job_id, j.job_name, w.workspace_name,
       g.run_type, g.trigger_type, g.result_state, g.termination_code,
       g.run_rows, g.distinct_runs, g.failed_run_rows,
       -- status: worst-first band on failed run rows per group (field heuristic; 5 / 20).
       CASE
         WHEN g.failed_run_rows >= 20 THEN 'CRITICAL'
         WHEN g.failed_run_rows >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM grouped g
LEFT JOIN latest_jobs j ON j.workspace_id = g.workspace_id AND j.job_id = g.job_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = g.workspace_id
ORDER BY failed_run_rows DESC
) q
