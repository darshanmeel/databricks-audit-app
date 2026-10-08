-- query_id: lakeflow_succeeded_with_failed_tasks
-- title: Runs that succeeded overall despite a failed task
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline
-- requires: SELECT on system.lakeflow; GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days; :warn_succeeded_with_failed (default 3) such runs for a job that flags WARN; :crit_succeeded_with_failed (default 10) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: the join key (job_task_run_timeline.job_run_id = job_run_timeline.run_id, plus workspace_id) and the end-row filter on both sides were verified against a live workspace.
-- read_this: One row = a job in the window. The column that matters is succeeded_runs_with_failed_task - runs where the overall job reported SUCCEEDED even though at least one of its tasks reported FAILED/ERROR/TIMED_OUT (usually an optional/best-effort task, or a masked failure worth a second look).
-- healthy: succeeded_runs_with_failed_task at/near 0 - field heuristic; tune :warn_succeeded_with_failed for your account.
-- investigate_if: succeeded_runs_with_failed_task at/above :warn_succeeded_with_failed (WARN) or :crit_succeeded_with_failed (CRITICAL) - field heuristic; check whether the failing task is intentionally marked non-blocking or is silently swallowing a real failure.
-- actions: 1) open one of the flagged runs and check which task failed and why (free); 2) if the task should block the run, remove its "optional/continue on failure" setting (config); 3) n/a - this is a correctness/visibility fix, not a spend decision.
-- next: lakeflow_failed_runs (for runs that failed outright, not just a masked task), lakeflow_tasks_near_timeout (a task timing out is one common cause of a masked task failure)
-- caveats: The documented join key is job_task_run_timeline.job_run_id = job_run_timeline.run_id (plus workspace_id). Both tables populate result_state only in the end row, so this filters on both sides before joining.
WITH job_end AS (
  SELECT workspace_id, job_id, run_id AS job_run_id, result_state AS job_result_state
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
),
task_end AS (
  SELECT workspace_id, job_id, job_run_id, task_key, result_state AS task_result_state
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND result_state IS NOT NULL
)
SELECT j.workspace_id, j.job_id,
       COUNT(DISTINCT j.job_run_id) AS succeeded_runs,
       COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT')
                           THEN j.job_run_id END) AS succeeded_runs_with_failed_task,
       -- status: worst-first band on succeeded-but-had-a-failed-task run count (field heuristic;
       -- :warn_succeeded_with_failed / :crit_succeeded_with_failed).
       CASE
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= :crit_succeeded_with_failed THEN 'CRITICAL'
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= :warn_succeeded_with_failed THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM job_end j
LEFT JOIN task_end t
  ON j.workspace_id = t.workspace_id AND j.job_id = t.job_id AND j.job_run_id = t.job_run_id
WHERE j.job_result_state = 'SUCCEEDED'
GROUP BY j.workspace_id, j.job_id
ORDER BY succeeded_runs_with_failed_task DESC
