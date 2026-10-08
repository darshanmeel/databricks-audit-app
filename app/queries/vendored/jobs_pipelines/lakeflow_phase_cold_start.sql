-- query_id: lakeflow_phase_cold_start
-- title: Job run phase breakdown - setup, queue, execution, cleanup
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline
-- requires: SELECT on system.lakeflow; GA (the five *_duration_seconds columns were added early Dec 2025)
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_setup_p95_s (default 60) 95th-percentile setup seconds that flags WARN; :crit_setup_p95_s (default 300) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: The five phase columns and the queue-vs-job_task_run_timeline scoping were verified against system.lakeflow.job_run_timeline in a live workspace. Per Databricks' own documentation, run_duration_seconds and the four phase columns on job_run_timeline are populated only for LEGACY SINGLE-TASK jobs; every multi-task job logs a literal 0 in ALL of them together (never NULL) - so on that table alone, a multi-task job could never be assessed for cold start. job_task_run_timeline carries its own task-level setup_duration_seconds (this folder's lakeflow_tasks_near_timeout already reads that table's execution_duration_seconds the same way, for every job, single- or multi-task - the only gap it or this query has is "not populated before late Nov/early Dec 2025", never a multi-task fold to 0), so this query now reads setup time from there instead, and a multi-task job gets a real cold-start read. job_task_run_timeline has no queue_duration_seconds column at all (task-level queue time is not published anywhere), so queue_s_p95 stays sourced from job_run_timeline and stays legacy-single-task-only - see caveats.
-- read_this: One row = a job in the window. The column that matters is setup_s_p95 - the 95th-percentile cold-start (cluster setup) seconds before a run's code starts executing, measured from every successful task run (single- or multi-task jobs alike); execution_s_total is how much of the run's time was actual work. A task that failed, for example while its cluster was starting, is left out of the percentile: lakeflow_failed_cluster_starts lists those by termination code.
-- healthy: setup_s_p95 below :warn_setup_p95_s seconds - field heuristic; tune :warn_setup_p95_s for your account.
-- investigate_if: setup_s_p95 at/above :warn_setup_p95_s (WARN) or :crit_setup_p95_s (CRITICAL) seconds - field heuristic; also check rows_setup_null before trusting a low number on a short-history account. queue_s_p95 stays NULL for a multi-task job - see caveats, this is a real data gap, not a bug.
-- actions: 1) switch the job to a job cluster/pool that is already warm, or share a cluster across tasks in the same job (free); 2) enable a cluster pool or serverless compute to cut cold-start time (config); 3) keep a small always-on pool of pre-warmed instances if setup latency is business-critical (spend).
-- next: lakeflow_failed_cluster_starts (task runs that failed before their code ran, by termination code), lakeflow_job_queue_time (queue time is one slice of this same breakdown, isolated, same legacy-single-task-only limit), lakeflow_workload_mix_hours (to see whether the affected job runs often enough to be worth the fix)
-- caveats: setup_s_total / setup_s_p95 / rows_setup_null / status are now computed from system.lakeflow.job_task_run_timeline's own setup_duration_seconds, one row per task run, summed/percentiled directly with no run_duration_seconds gate: that table has no "0 for every multi-task job" fold, only "NULL before this column existed" (late Nov/early Dec 2025), so a genuine 0-second setup reads as a real 0 and a multi-task job is judged on its own tasks' cold starts instead of reading NOT_ASSESSED forever. rows_setup_null counts task rows whose setup_duration_seconds is NULL (column not yet populated); a job with no task row in the window at all has nothing to band on and reads NOT_ASSESSED, distinct from a job whose observed task rows all have a NULL setup (also NOT_ASSESSED, via the p95-is-NULL branch). queue_s_total / queue_s_p95 are UNCHANGED and still come from job_run_timeline's run-level queue_duration_seconds, which Databricks documents as populated only for LEGACY SINGLE-TASK jobs (every multi-task job's run-level row logs a literal 0, not NULL, in it and its four siblings together) - job_task_run_timeline has no queue_duration_seconds column at all, at any granularity, so a multi-task job's queue wait cannot be measured from any system table today; this query reads queue_duration_seconds only where run_duration_seconds > 0 (`CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END`), keeping a genuine 0-second queue on a legacy single-task job as a real 0 while a multi-task row's sentinel 0 stays excluded as "not reported" - queue_s_p95 is simply NULL for a multi-task-only job, a permanent gap this query cannot close, not a NOT_ASSESSED status (this id has no status branch on queue at all - see lakeflow_job_queue_time for the dedicated, banded queue check, with the same limit). execution_s_total / cleanup_s_total / run_s_total are also UNCHANGED, still run-level from job_run_timeline and still 0-for-multi-task (read them as "legacy single-task jobs only" the same way queue is). All five *_duration_seconds columns on both tables are NULL before their own column existed, so on a short-history account rows_setup_null / a NULL queue_s_p95 may just mean "not populated yet", not "multi-task". This query uses the exact PERCENTILE function; on large volumes prefer PERCENTILE_APPROX instead, since exact PERCENTILE can be expensive.
WITH run_level AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT run_id)                                                      AS runs,
         SUM(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END)     AS queue_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN execution_duration_seconds END) AS execution_s_total,
         SUM(CASE WHEN run_duration_seconds > 0 THEN cleanup_duration_seconds END)   AS cleanup_s_total,
         SUM(run_duration_seconds)                                                   AS run_s_total,
         PERCENTILE(CASE WHEN run_duration_seconds > 0 THEN queue_duration_seconds END, 0.95) AS queue_s_p95
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL     -- end row carries the final durations
  GROUP BY workspace_id, job_id
),
-- Task-level setup time: real on every job, single- or multi-task alike (unlike job_run_timeline's
-- own setup_duration_seconds, which is a literal 0 - not NULL - on every multi-task job's run row).
task_setup AS (
  SELECT workspace_id, job_id,
         COUNT(*)                                              AS task_rows,
         SUM(setup_duration_seconds)                           AS setup_s_total,
         PERCENTILE(setup_duration_seconds, 0.95)               AS setup_s_p95,
         SUM(CASE WHEN setup_duration_seconds IS NULL THEN 1 ELSE 0 END) AS rows_setup_null
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
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
       -- :warn_setup_p95_s / :crit_setup_p95_s), from task_setup so a multi-task job is judged
       -- too. A job with no task row in the window has nothing to band on -> NOT_ASSESSED,
       -- distinct from every observed task's setup folding to NULL (also NOT_ASSESSED, via the
       -- p95-is-NULL branch below).
       CASE
         WHEN ts.task_rows IS NULL OR ts.task_rows = 0 THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 IS NULL THEN 'NOT_ASSESSED'
         WHEN ts.setup_s_p95 >= :crit_setup_p95_s THEN 'CRITICAL'
         WHEN ts.setup_s_p95 >= :warn_setup_p95_s THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM run_level r
LEFT JOIN task_setup ts ON r.workspace_id = ts.workspace_id AND r.job_id = ts.job_id
ORDER BY setup_s_p95 DESC
