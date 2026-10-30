-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_workload_mix_hours.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, job_id, run_type, trigger_type, run_day,
       COUNT(DISTINCT run_id) AS distinct_runs,
       SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS completed_run_rows,
       SUM(execution_duration_seconds) AS execution_s_total
FROM (
  -- each run counts once, on the day it started, even when its hourly rows cross midnight
  SELECT workspace_id, job_id, run_id, run_type, trigger_type, result_state, execution_duration_seconds,
         date_trunc('DAY', MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id)) AS run_day
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_start_time < date_trunc('DAY', __AS_OF_TS__)
) r
GROUP BY workspace_id, job_id, run_type, trigger_type, run_day
ORDER BY workspace_id, run_day DESC
) q
