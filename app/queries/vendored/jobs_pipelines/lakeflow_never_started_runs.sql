-- query_id: lakeflow_never_started_runs
-- title: Runs that never started execution
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_never_started (default 3) never-started runs for a (job, termination_code) pair that flags WARN; :crit_never_started (default 10) that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: The period_start_time = period_end_time signature for a never-executed run, and termination_code as the reason, were verified against system.lakeflow.job_run_timeline in a live workspace. never_started_runs_invalid_config_no_tasks (an INVALID_RUN_CONFIGURATION run with no row at all in system.lakeflow.job_task_run_timeline) is a new, UNCONFIRMED second signature, added because it was not known whether every never-started run is zero-length: it has not been checked against a live account's actual INVALID_RUN_CONFIGURATION rows. Read never_started_runs_zero_length and never_started_runs_invalid_config_no_tasks separately before trusting the combined never_started_runs: if the invalid-config count is consistently 0, the zero-length match alone was already sufficient; if it consistently matches or exceeds the zero-length count for the same termination_code, the two may be catching the same runs under different names rather than a genuine second case.
-- read_this: One row = a (workspace, job, termination_code) combination. never_started_runs is the DISTINCT union of never_started_runs_zero_length (a zero-length run period) and never_started_runs_invalid_config_no_tasks (an INVALID_RUN_CONFIGURATION run that spawned no task row at all) - runs that were created but never began executing (queue rejection, quota hit, config error) before terminating.
-- healthy: never_started_runs at/near 0 for every job - field heuristic; tune :warn_never_started for your account.
-- investigate_if: never_started_runs at/above :warn_never_started (WARN) or :crit_never_started (CRITICAL) for a job/termination_code pair - field heuristic; check termination_code first, it usually names the blocker (queue/quota/config).
-- actions: 1) read the termination_code and, if it is a quota/limit code, check for a scheduling pile-up on that job (free); 2) fix the underlying config error or raise the relevant workspace limit (config); 3) if the block is capacity (queue/cluster limits), add capacity (spend).
-- next: lakeflow_termination_taxonomy (for the account-wide termination_code picture), lakeflow_job_queue_time (queueing and never-started runs often share a capacity root cause)
-- caveats: period_start_time == period_end_time marks a run that never executed; termination_code gives the reason. This is filtered to end rows only, to avoid false positives from clock-hour-aligned slicing of long runs. never_started_runs_invalid_config_no_tasks additionally counts a run whose termination_code is INVALID_RUN_CONFIGURATION and which has no matching row in system.lakeflow.job_task_run_timeline in the window - a run that never started should never have spawned a task; this can overlap never_started_runs_zero_length (a run can match both), so the combined never_started_runs column is a DISTINCT run_id union, never a sum of the two. job_name comes from system.lakeflow.jobs (SCD2, latest row by change_time, deleted jobs kept) and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately.
WITH runs AS (
  SELECT workspace_id, job_id, run_id, termination_code, period_start_time, period_end_time
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_rows AS (
  SELECT DISTINCT workspace_id, job_id, job_run_id AS run_id
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
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
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
)
SELECT g.workspace_id, g.job_id, j.job_name, w.workspace_name, g.termination_code,
       g.never_started_runs_zero_length, g.never_started_runs_invalid_config_no_tasks,
       g.never_started_runs,
       -- status: worst-first band on never-started run count (field heuristic; :warn_never_started / :crit_never_started).
       CASE
         WHEN g.never_started_runs >= :crit_never_started THEN 'CRITICAL'
         WHEN g.never_started_runs >= :warn_never_started THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM grouped g
LEFT JOIN latest_jobs j ON j.workspace_id = g.workspace_id AND j.job_id = g.job_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = g.workspace_id
ORDER BY never_started_runs DESC
