-- query_id: lakeflow_daily_state
-- title: Each job and pipeline, day by day: ran fine, failed, ran slow or skipped
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.pipeline_update_timeline
-- requires: SELECT on system.lakeflow; GA
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :slow_ratio (default 1.5) a day is slow
--   when its average run takes this many times the resource's own median run in the window;
--   :slow_min_s (default 60) and at least this many seconds longer
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Uses the same end-row rules as
--   perf_daily_by_resource; confirm one job's runs and failed add up to its rows there.
-- read_this: One row = one day for one job or pipeline that finished at least one run (a job run
--   or a pipeline update) that day. runs, failed and skipped count those runs by final result
--   (failed = FAILED, ERROR or TIMED_OUT; skipped = SKIPPED or CANCELED); timed_runs are the
--   rest, total_run_s their summed duration, avg_run_s their average and max_run_s the longest;
--   median_run_s is the resource's own median run over the whole window.
--   day_state reads failed when any run failed, slow when avg_run_s is at least :slow_ratio times
--   median_run_s and :slow_min_s longer, skipped when every run was skipped, else ok. Days with
--   no run have no row. failed_days and slow_days count the resource's failed and slow days in
--   the window; trouble_rank orders resources worst first (most failed days, then slow days, then
--   failed runs), and rows come in that order, so the first rows are the worst resources.
--   job_id and pipeline_id repeat resource_id for that type, so a tag filter reaches the job's
--   or pipeline's own tags.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory (Jobs > Failures shows it as one state line per job)
-- actions: n/a - inventory (see lakeflow_job_reliability and lakeflow_job_duration_regression)
-- next: lakeflow_job_reliability (failure rate and streak per job), lakeflow_job_duration_regression
--   (a job slower than its own baseline), perf_failure_causes_daily (why runs failed)
-- caveats: GRAIN - usage_date, resource_type, resource_key. A run counts on the day it started and
--   only once it has finished, before today; a repaired run counts once, with its final result and
--   duration. The current day is excluded.
WITH run_rows AS (
  SELECT workspace_id, job_id, run_id, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0) AS run_s_reported,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, job_id, run_id) AS run_start
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
),
job_runs AS (
  SELECT 'job' AS resource_type, workspace_id, job_id AS resource_id, date(run_start) AS usage_date,
         result_state,
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, period_end_time)) AS run_s
  FROM run_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
),
update_rows AS (
  SELECT workspace_id, pipeline_id, update_id, period_end_time, result_state,
         MIN(period_start_time) OVER (PARTITION BY workspace_id, pipeline_id, update_id) AS update_start
  FROM system.lakeflow.pipeline_update_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
),
updates AS (
  SELECT 'pipeline' AS resource_type, workspace_id, pipeline_id AS resource_id, date(update_start) AS usage_date,
         result_state,
         timestampdiff(SECOND, update_start, period_end_time) AS run_s
  FROM update_rows
  WHERE result_state IS NOT NULL
    AND period_end_time < date_trunc('DAY', current_timestamp())
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, pipeline_id, update_id ORDER BY period_end_time DESC) = 1
),
all_runs AS (
  SELECT * FROM job_runs
  UNION ALL SELECT * FROM updates
),
typical AS (
  SELECT resource_type, workspace_id, resource_id, percentile(run_s, 0.5) AS median_run_s
  FROM all_runs
  WHERE result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED')
  GROUP BY resource_type, workspace_id, resource_id
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         COUNT(*) AS runs,
         SUM(CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END) AS failed,
         SUM(CASE WHEN result_state IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN 1 ELSE 0 END) AS skipped,
         SUM(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN 1 ELSE 0 END) AS timed_runs,
         SUM(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS total_run_s,
         MAX(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS max_run_s,
         AVG(CASE WHEN result_state NOT IN ('SKIPPED', 'CANCELED', 'CANCELLED') THEN run_s END) AS avg_run_s
  FROM all_runs
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
stated AS (
SELECT d.usage_date,
       d.resource_type,
       concat(COALESCE(d.workspace_id, 'account'), ':', d.resource_id) AS resource_key,
       d.workspace_id,
       d.resource_id,
       d.runs,
       d.failed,
       d.skipped,
       d.timed_runs,
       ROUND(COALESCE(d.total_run_s, 0), 1) AS total_run_s,
       ROUND(d.max_run_s, 1) AS max_run_s,
       ROUND(d.avg_run_s, 1) AS avg_run_s,
       ROUND(t.median_run_s, 1) AS median_run_s,
       CASE
         WHEN d.failed > 0 THEN 'failed'
         WHEN d.avg_run_s IS NOT NULL AND t.median_run_s > 0
              AND d.avg_run_s >= :slow_ratio * t.median_run_s
              AND d.avg_run_s - t.median_run_s >= :slow_min_s THEN 'slow'
         WHEN d.skipped = d.runs THEN 'skipped'
         ELSE 'ok'
       END AS day_state
FROM daily d
LEFT JOIN typical t
  ON  t.resource_type = d.resource_type AND t.workspace_id IS NOT DISTINCT FROM d.workspace_id
  AND t.resource_id = d.resource_id
),
per_resource AS (
  SELECT stated.*,
         SUM(CASE WHEN day_state = 'failed' THEN 1 ELSE 0 END) OVER (PARTITION BY resource_type, resource_key) AS failed_days,
         SUM(CASE WHEN day_state = 'slow' THEN 1 ELSE 0 END) OVER (PARTITION BY resource_type, resource_key) AS slow_days,
         SUM(failed) OVER (PARTITION BY resource_type, resource_key) AS resource_failed
  FROM stated
)
SELECT usage_date, resource_type, resource_key, workspace_id, resource_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       runs, failed, skipped,
       timed_runs, total_run_s, max_run_s, avg_run_s, median_run_s, day_state, failed_days, slow_days,
       DENSE_RANK() OVER (ORDER BY failed_days DESC, slow_days DESC, resource_failed DESC,
                                   resource_type, resource_key) AS trouble_rank
FROM per_resource
ORDER BY trouble_rank, usage_date
