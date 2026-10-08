-- query_id: lakeflow_failed_cluster_starts
-- title: Job task runs that failed before their code ran, by job and termination code
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_task_run_timeline
-- requires: SELECT on system.lakeflow; GA (setup_duration_seconds and execution_duration_seconds
--   are filled from late Nov / early Dec 2025)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. A failed start is read as a task run
--   whose final result failed with a setup time recorded and no execution time; confirm on a few
--   runs' task pages that they failed while their cluster was starting.
-- read_this: One row = one job and termination code. failed_starts counts task runs that ended
--   FAILED, ERROR or TIMED_OUT with a setup time recorded and no execution time: they failed
--   before their code ran, most often while the cluster was starting (CLOUD_FAILURE,
--   CLUSTER_ERROR, DRIVER_ERROR and the like). setup_s_total is the time they spent starting;
--   first_failed_at and last_failed_at bound them. lakeflow_phase_cold_start leaves these task
--   runs out of its cold-start percentile.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (see Jobs > Slow & queued)
-- next: lakeflow_phase_cold_start (start-up time of the task runs that succeeded),
--   lakeflow_job_reliability (each job's failure rate and streak)
-- caveats: GRAIN - workspace_id, job_id, termination_code. A task run counts once, on its final
--   row; a task retried after a failed start counts its failed attempt here and its successful
--   one in the cold start. Rows written before the duration columns were filled have no setup
--   time and are left out. The current day is excluded.
WITH final_tasks AS (
  SELECT workspace_id, job_id, period_start_time, period_end_time, result_state,
         COALESCE(NULLIF(termination_code, ''), 'unknown') AS termination_code,
         setup_duration_seconds, execution_duration_seconds
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
)
SELECT workspace_id,
       job_id,
       termination_code,
       COUNT(*)                    AS failed_starts,
       SUM(setup_duration_seconds) AS setup_s_total,
       MIN(period_start_time)      AS first_failed_at,
       MAX(period_end_time)        AS last_failed_at
FROM final_tasks
WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  AND setup_duration_seconds IS NOT NULL
  AND COALESCE(execution_duration_seconds, 0) = 0
GROUP BY workspace_id, job_id, termination_code
ORDER BY failed_starts DESC, setup_s_total DESC, workspace_id, job_id, termination_code
