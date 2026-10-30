-- query_id: lakeflow_tasks_near_timeout
-- title: Tasks running near or past their configured timeout
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_task_run_timeline, system.lakeflow.job_tasks, system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (execution_duration_seconds and timeout_seconds were both added late Nov 2025)
-- empty_if: schema_not_enabled, submit_run_skipped
-- params: :period_days (default 30) rolling window in days; :near_timeout_ratio (default 0.8) fraction of the configured timeout that counts as "near timeout"; :warn_near_timeout_runs (default 3) near/over-timeout runs for a task that flags WARN; :crit_near_timeout_runs (default 10) that flags CRITICAL; :null_bucket_material_share (default 0.5) share of a task's runs that must be runs_no_task_timeout or runs_exec_null before it reads NOT_ASSESSED instead of being banded
-- confidence: needs_confirmation
-- confidence_note: both execution_duration_seconds (job_task_run_timeline) and timeout_seconds (job_tasks) are not populated before late Nov 2025; runs_no_task_timeout / runs_exec_null expose that so a short-history account degrades to "not assessed" instead of reading a missing value as zero.
-- read_this: One row = a task within a job. The columns that matter are runs_near_timeout (execution reached at least :near_timeout_ratio of the configured timeout but stayed under it) and runs_over_timeout (execution reached or passed the timeout and should have been killed) - the two never overlap, so status bands on their plain sum. runs_no_timeout_bound is runs with an explicit 0-second timeout (no bound configured, not a gap); runs_no_task_timeout is runs whose timeout_seconds is NULL (not yet populated on that row).
-- healthy: runs_near_timeout and runs_over_timeout both at/near 0 - field heuristic; tune :near_timeout_ratio and :warn_near_timeout_runs for your account.
-- investigate_if: (runs_near_timeout + runs_over_timeout) at/above :warn_near_timeout_runs (WARN) or :crit_near_timeout_runs (CRITICAL) - field heuristic; runs_over_timeout > 0 is the more urgent signal since the task should already have been killed. NOT_ASSESSED is not a pass: it means runs_no_task_timeout or runs_exec_null makes up at least :null_bucket_material_share of task_runs - read not_assessed_reason.
-- actions: 1) look at whether the task's input volume grew and the timeout was never revisited (free); 2) raise the task's configured timeout to a realistic value, or split the task into smaller steps (config); 3) if the task is legitimately compute-bound, give it a faster node type or more parallelism (spend).
-- next: lakeflow_job_tasks_no_timeout (the tasks with no timeout at all, a related gap), lakeflow_phase_cold_start (for the run-level setup/queue/execution breakdown)
-- not_assessed_reasons: timeout_not_populated: most of this task's runs have a NULL (not yet populated) timeout_seconds, so a near/over-timeout verdict cannot be judged; exec_duration_not_populated: most of this task's runs have a NULL (not yet populated) execution_duration_seconds, for the same reason
-- caveats: "Near timeout" means observed execution_duration_seconds reaches :near_timeout_ratio of the task's configured timeout_seconds but stays under it; "over timeout" means it ran at/past the timeout - the two conditions are mutually exclusive (near is now bounded above by the timeout itself), so runs_near_timeout + runs_over_timeout never double-counts a run in both buckets. Both source columns are not populated before late Nov 2025. A timeout_seconds of exactly 0 is an explicit "no bound configured" (runs_no_timeout_bound) - a deliberate setting, not missing data, and is excluded from near/over-timeout the same as a NULL timeout since there is nothing to compare against; a NULL timeout_seconds (runs_no_task_timeout) or a NULL execution_duration_seconds (runs_exec_null) is column-not-yet-populated and, when either accounts for at least :null_bucket_material_share of a task's runs, degrades the whole row to NOT_ASSESSED rather than silently excluding those runs with no signal. job_id and task_key are unique only within a workspace+job, so the join is on (workspace_id, job_id, task_key); job_tasks is SCD2, so this takes the latest row per (workspace_id, job_id, task_key) by change_time before joining, to avoid fan-out. job_task_run_timeline's result_state is populated only in the task's end row, so this filters to result_state IS NOT NULL. job_name comes from system.lakeflow.jobs (SCD2, latest row by change_time, deleted jobs kept) and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately. There are no dollars here - durations are not a billing unit.
WITH latest_tasks AS (
  SELECT workspace_id, job_id, task_key, timeout_seconds, delete_time
  FROM system.lakeflow.job_tasks
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, task_key ORDER BY change_time DESC
  ) = 1
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
task_end AS (
  SELECT workspace_id, job_id, task_key, run_id, execution_duration_seconds
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- task end row only
),
agg AS (
  SELECT t.workspace_id, t.job_id, t.task_key,
         lt.timeout_seconds,
         COUNT(DISTINCT t.run_id)                              AS task_runs,
         MAX(t.execution_duration_seconds)                     AS max_exec_s,
         percentile(t.execution_duration_seconds, 0.95) AS exec_s_p95,
         -- runs whose execution reached >= :near_timeout_ratio of a CONFIGURED (>0) timeout but
         -- stayed under it -- mutually exclusive with runs_over_timeout, so the two never overlap.
         SUM(CASE WHEN lt.timeout_seconds > 0
                   AND t.execution_duration_seconds >= :near_timeout_ratio * lt.timeout_seconds
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
       -- :warn_near_timeout_runs / :crit_near_timeout_runs), degraded to NOT_ASSESSED when either
       -- NULL bucket accounts for a material share of the task's runs.
       CASE
         WHEN a.runs_no_task_timeout >= :null_bucket_material_share * a.task_runs THEN 'NOT_ASSESSED'
         WHEN a.runs_exec_null       >= :null_bucket_material_share * a.task_runs THEN 'NOT_ASSESSED'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= :crit_near_timeout_runs THEN 'CRITICAL'
         WHEN (a.runs_near_timeout + a.runs_over_timeout) >= :warn_near_timeout_runs THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.runs_no_task_timeout >= :null_bucket_material_share * a.task_runs THEN 'timeout_not_populated'
         WHEN a.runs_exec_null       >= :null_bucket_material_share * a.task_runs THEN 'exec_duration_not_populated'
       END AS not_assessed_reason
FROM agg a
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = a.workspace_id
ORDER BY (a.runs_near_timeout + a.runs_over_timeout) DESC
