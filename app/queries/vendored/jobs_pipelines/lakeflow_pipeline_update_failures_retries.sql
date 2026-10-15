-- query_id: lakeflow_pipeline_update_failures_retries
-- title: Pipeline update failures and retry triggers
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.pipeline_update_timeline
-- requires: SELECT on system.lakeflow; Public Preview (system.lakeflow.pipeline_update_timeline)
-- empty_if: schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_failed_updates (default 3) failed update rows in a group that flags WARN; :crit_failed_updates (default 10) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: result_state values (COMPLETED/FAILED/CANCELED - one L), update_type values (FULL_REFRESH/REFRESH/VALIDATE), and trigger_type='RETRY_ON_FAILURE' were verified against system.lakeflow.pipeline_update_timeline in a live workspace.
-- read_this: One row = a (workspace, pipeline, update_type, trigger_type, result_state) combination. The column that matters is failed_update_rows; retry_triggered_rows next to it tells you how many of the group's updates were themselves a retry, not an original attempt.
-- healthy: failed_update_rows low relative to updates, with retry_triggered_rows explaining most of any repeats - field heuristic; tune :warn_failed_updates for your account.
-- investigate_if: failed_update_rows at/above :warn_failed_updates (WARN) or :crit_failed_updates (CRITICAL) for a pipeline - field heuristic; a pipeline that fails and keeps auto-retrying without ever completing is the priority case.
-- actions: 1) open the pipeline's update history and read the failure reason on the most recent FAILED update (free); 2) fix the upstream data/schema issue or expectation causing the failure (config); 3) if failures trace back to under-provisioned compute, resize the pipeline's cluster (spend).
-- next: lakeflow_pipeline_cost (to see the DBU cost of a pipeline that keeps retrying), lakeflow_pipeline_idle_tail_duration (for the same pipeline's idle/active time split)
-- caveats: This table is Public Preview - it degrades to empty/absent if disabled on your account. result_state is populated only in the update's end row, so this filters to result_state IS NOT NULL. Confirmed values: result_state is COMPLETED/FAILED/CANCELED (one L, not CANCELLED); update_type is FULL_REFRESH/REFRESH/VALIDATE; trigger_type='RETRY_ON_FAILURE' is the confirmed retry signal. request_id (not selected here) is what groups a retried/restarted update back to its original attempt, if you need to trace a specific retry chain.
WITH end_rows AS (
  SELECT workspace_id, pipeline_id, update_id, request_id, update_type,
         trigger_type, result_state, period_start_time, period_end_time
  FROM system.lakeflow.pipeline_update_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())
    AND result_state IS NOT NULL          -- end row only for updates >1h
)
SELECT e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state,
       COUNT(DISTINCT e.update_id) AS updates,
       SUM(CASE WHEN e.result_state = 'FAILED'             THEN 1 ELSE 0 END) AS failed_update_rows,
       SUM(CASE WHEN e.trigger_type = 'RETRY_ON_FAILURE'   THEN 1 ELSE 0 END) AS retry_triggered_rows,
       -- status: worst-first band on failed update rows per group (field heuristic; :warn_failed_updates / :crit_failed_updates).
       CASE
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= :crit_failed_updates THEN 'CRITICAL'
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= :warn_failed_updates THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows e
GROUP BY e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state
ORDER BY failed_update_rows DESC
