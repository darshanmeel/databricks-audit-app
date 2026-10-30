-- query_id: lakeflow_termination_type_probe
-- title: Termination type discovery probe (runtime value enumeration)
-- domain: jobs_pipelines   tier: lite
-- reads: system.lakeflow.job_run_timeline
-- requires: SELECT on system.lakeflow; GA (the column exists; its value enum is unverified - see caveats)
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: termination_type is confirmed to exist as a column, but its distinct value list is unverified and it is not populated before early Dec 2025; this query is a runtime discovery probe rather than one that assumes any specific value.
-- read_this: One row = a (workspace, job, termination_type) combination in the window, with a raw row count. Run this once to discover the actual termination_type values on your account before building any logic that filters or bands on them.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: lakeflow_termination_taxonomy (the confirmed termination_code taxonomy, safe to use today), lakeflow_failed_runs (drill into runs behind a given termination_type once confirmed)
-- caveats: termination_type is confirmed to exist, but its distinct value list is unverified on this account, and the column is not populated before early Dec 2025. This is deliberately a runtime discovery probe: it groups by the raw column and hardcodes no assumed values. Confirm the values you see here before relying on termination_type elsewhere.
-- NEEDS CONFIRMATION: termination_type column exists but its value enum is UNVERIFIED.
-- This probe discovers distinct values at runtime; confirm the column populates on the account.
SELECT workspace_id, job_id, termination_type, COUNT(*) AS run_rows
FROM system.lakeflow.job_run_timeline
WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  AND result_state IS NOT NULL
GROUP BY workspace_id, job_id, termination_type
ORDER BY workspace_id, run_rows DESC
