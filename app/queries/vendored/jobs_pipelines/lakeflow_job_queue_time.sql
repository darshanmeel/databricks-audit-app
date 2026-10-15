-- query_id: lakeflow_job_queue_time
-- title: Job queue time by workspace and job
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline
-- requires: SELECT on system.lakeflow; GA (queue_duration_seconds was added late Nov 2025)
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_queue_p95_s (default 60) 95th-percentile queue seconds that flags WARN; :crit_queue_p95_s (default 300) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: queue_duration_seconds is not populated before late Nov 2025, so on a short-history account it is NULL for every run; runs_queue_null exposes that so this query degrades instead of reading NULL as zero queue time. Per Databricks' own documentation, queue_duration_seconds (like the other four *_duration_seconds columns on this table) is populated only for LEGACY SINGLE-TASK jobs; every multi-task job logs a literal 0 in it (never NULL). run_duration_seconds is documented with the same legacy-single-task-only scope, so it is 0 on the exact same multi-task rows - this query reads queue_duration_seconds only where run_duration_seconds > 0, so a genuine 0-second queue on a legacy single-task job is kept as a real 0, and a multi-task row's 0 is folded to "not reported" instead - see caveats.
-- read_this: One row = a job in the window. The column that matters is queue_s_p95 - the 95th-percentile seconds a run waited before starting; queue_s_total is the sum across all its runs. Consistently high p95 on the same job means it is waiting on capacity, not a one-off traffic spike.
-- healthy: queue_s_p95 below :warn_queue_p95_s seconds - field heuristic; tune :warn_queue_p95_s for your account.
-- investigate_if: queue_s_p95 at/above :warn_queue_p95_s (WARN) or :crit_queue_p95_s (CRITICAL) seconds - field heuristic; also check runs_queue_null before trusting a "0" queue time on a short-history account.
-- actions: 1) stagger the job's schedule off the top of the hour and away from other jobs sharing the same warehouse (free); 2) move the job onto its own job cluster or a warehouse with autoscaling headroom (config); 3) add cluster capacity or move to serverless so runs stop queuing behind each other (spend).
-- next: lakeflow_phase_cold_start (for the full setup/queue/execution/cleanup breakdown), lakeflow_stale_zombie_jobs (if the job turns out to rarely run, queuing may not be worth fixing)
-- caveats: queue_duration_seconds is not populated before late Nov 2025, so on a short-history account it is NULL for every run. Databricks documents that this column (and its four siblings on job_run_timeline, including run_duration_seconds itself) is populated only for legacy SINGLE-TASK jobs; every MULTI-TASK job's run-level row logs a literal 0, not NULL, in ALL of them together - so run_duration_seconds > 0 tells a legacy single-task run apart from a multi-task run ROW BY ROW. This query reads queue_duration_seconds only on rows where run_duration_seconds > 0 (`CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END`, not a blind NULLIF(queue_duration_seconds, 0)): a genuine 0-second queue on a legacy single-task job is kept as a real 0 and banded as OK, while a multi-task row's 0 is excluded as "not reported", the same convention lakeflow_long_running_runs already uses for its own wall-clock fallback and lakeflow_phase_cold_start now uses for its four phase columns. runs_queue_null counts a row as null when EITHER run_duration_seconds is not > 0 (multi-task, or no real wall-clock time) OR queue_duration_seconds itself is NULL (column not yet populated on a legacy job), so a fully-degraded column still reads "not assessed - column not yet populated / not reported for multi-task jobs" rather than zero queue time. A job with no run in the window reporting run_duration_seconds > 0 has nothing to band on at all and reads NOT_ASSESSED. queue_duration_seconds lives ONLY on job_run_timeline, not on job_task_run_timeline. It is populated only in the run's end row (runs over 1h are sliced hourly), so this filters to result_state IS NOT NULL. job_id is unique only within a workspace, so all per-job grouping is on (workspace_id, job_id). PERCENTILE takes a fraction in [0,1]; 0.95 is correct here. There are no dollars: queue seconds are not a billing unit, and Databricks publishes no DBU-to-dollar rate.
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, queue_duration_seconds, run_duration_seconds
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
)
SELECT workspace_id, job_id,
       COUNT(DISTINCT run_id)                                   AS distinct_runs,
       SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)                   AS queue_s_total,
       percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95,
       SUM(CASE WHEN run_duration_seconds > 0 AND queue_duration_seconds IS NOT NULL THEN 0 ELSE 1 END) AS runs_queue_null,
       -- status: worst-first band on p95 queue seconds (field heuristic; :warn_queue_p95_s / :crit_queue_p95_s).
       -- A job with no run reporting real wall-clock time has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed run's queue phase folding to NULL (also NOT_ASSESSED, via
       -- the p95-is-NULL branch below). Reading queue_duration_seconds only where
       -- run_duration_seconds > 0 keeps a genuine 0-second queue on a legacy single-task job as a
       -- real 0 (OK), instead of NULLIF(queue_duration_seconds, 0) folding EVERY 0 to NULL and
       -- inflating p95 off queued runs alone.
       CASE
         WHEN SUM(CASE WHEN run_duration_seconds > 0 THEN 1 ELSE 0 END) = 0 THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) IS NULL THEN 'NOT_ASSESSED'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= :crit_queue_p95_s THEN 'CRITICAL'
         WHEN percentile(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) >= :warn_queue_p95_s THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows
GROUP BY workspace_id, job_id
ORDER BY queue_s_p95 DESC
