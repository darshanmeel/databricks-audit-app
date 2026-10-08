-- generated from dbt/models/databricks_direct/performance/d_perf_failure_causes_daily.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/perf_failure_causes_daily.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH stmt_fail AS (
  SELECT 'warehouse' AS resource_type, workspace_id, compute.warehouse_id AS resource_id, date(start_time) AS usage_date,
         COALESCE(NULLIF(regexp_extract(error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1), ''), 'unclassified') AS cause
  FROM `system`.`query`.`history`
  WHERE execution_status = 'FAILED'
    AND compute.warehouse_id IS NOT NULL
    AND start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND start_time < __AS_OF_DATE__
),
run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, termination_code,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
),
final_runs AS (
  SELECT workspace_id, job_id, run_start, result_state, termination_code
  FROM run_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
run_fail AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         COALESCE(NULLIF(termination_code, ''), 'unknown') AS cause
  FROM final_runs
  WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
),
fails AS (
  SELECT * FROM stmt_fail
  UNION ALL
  SELECT * FROM run_fail
)
SELECT usage_date,
       resource_type,
       concat(COALESCE(workspace_id, 'account'), ':', resource_id) AS resource_key,
       workspace_id,
       resource_id,
       cause,
       COUNT(*) AS failures
FROM fails
GROUP BY usage_date, resource_type, workspace_id, resource_id, cause
ORDER BY usage_date DESC, resource_type, resource_key, cause
) q
