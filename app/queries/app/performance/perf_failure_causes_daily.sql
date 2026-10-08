-- query_id: perf_failure_causes_daily
-- title: Daily failed queries per warehouse and failed runs per job, by cause
-- domain: performance   tier: standard
-- reads: system.query.history, system.lakeflow.job_run_timeline
-- requires: SELECT on system.query and system.lakeflow; GA
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Error classes are read the way
--   query_failed_statements_grouped reads them; termination codes the way lakeflow_job_reliability
--   counts a failed run.
-- read_this: One row = one day's failures of one cause for one SQL warehouse or job. resource_key
--   is workspace_id:resource_id. For a warehouse, cause is the statement's error class (the leading
--   [ERROR_CLASS] token of its error message, or unclassified); for a job, it is the termination
--   code of a run whose final result was FAILED, ERROR or TIMED_OUT (unknown when empty). failures
--   counts them. Use it next to perf_daily_by_resource to see which cause rose or fell after a
--   change.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (see Cost > Before & after)
-- next: query_failed_statements_grouped (the failing statements by source),
--   lakeflow_job_reliability (each job's failure rate and streak)
-- caveats: GRAIN - usage_date, resource_type, resource_key, cause. Statements on SQL warehouses
--   only; a cancelled statement is not a failure. A job run counts on the day it started, once,
--   with its final result. No error message text is kept, only its class. The current day is
--   excluded.
WITH stmt_fail AS (
  SELECT 'warehouse' AS resource_type, workspace_id, compute.warehouse_id AS resource_id, date(start_time) AS usage_date,
         COALESCE(NULLIF(regexp_extract(error_message, concat('^', chr(92), '[([A-Za-z0-9_.]+)', chr(92), ']'), 1), ''), 'unclassified') AS cause
  FROM system.query.history
  WHERE execution_status = 'FAILED'
    AND compute.warehouse_id IS NOT NULL
    AND start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
),
run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, termination_code,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
),
final_runs AS (
  SELECT workspace_id, job_id, run_start, result_state, termination_code
  FROM run_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
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
