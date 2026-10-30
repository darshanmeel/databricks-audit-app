-- query_id: lakeflow_termination_taxonomy
-- title: Termination code taxonomy across job runs
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.job_run_timeline
-- requires: SELECT on system.lakeflow; GA
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: the documented termination_code value set (root-cause classification plus quota/limit-hit codes such as WORKSPACE_RUN_LIMIT_EXCEEDED, MAX_JOB_QUEUE_SIZE_EXCEEDED, CLUSTER_ERROR, STORAGE_ACCESS_ERROR) was verified against a live workspace.
-- read_this: One row = a (workspace, job, termination_code) combination in the window. Use run_rows / distinct_runs to see which termination reasons dominate before drilling into the specific failing jobs.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: lakeflow_failed_runs (drill into which jobs/triggers a given termination_code hits), lakeflow_failed_jobs_wasted_dbus (drill into the DBU cost of jobs terminating this way)
-- caveats: The documented termination_code values cover root-cause classification plus quota/limit-hit detection (for example WORKSPACE_RUN_LIMIT_EXCEEDED, MAX_JOB_QUEUE_SIZE_EXCEEDED, CLUSTER_ERROR, STORAGE_ACCESS_ERROR). The separate termination_type column is intentionally excluded here - its value list is unverified; see lakeflow_termination_type_probe.
SELECT workspace_id, job_id, termination_code,
       COUNT(*)              AS run_rows,
       COUNT(DISTINCT run_id) AS distinct_runs
FROM system.lakeflow.job_run_timeline
WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  AND period_end_time < date_trunc('DAY', current_timestamp())
  AND result_state IS NOT NULL          -- end row only
  AND termination_code IS NOT NULL
GROUP BY workspace_id, job_id, termination_code
ORDER BY workspace_id, run_rows DESC
