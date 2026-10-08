-- query_id: lakeflow_job_recent_runs
-- title: Last N runs per job - start, duration, result, failing task and cost
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.lakeflow.jobs, system.access.workspaces_latest, system.billing.usage, system.billing.list_prices, system.query.history
-- requires: SELECT on system.lakeflow, system.billing, system.query AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (system.lakeflow.job_run_timeline/job_task_run_timeline/jobs and system.billing are generally available; system.query.history is Public Preview)
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days, by run_start; :runs_per_job (default 10) how many of each job's most recent runs to keep
-- confidence: needs_confirmation
-- confidence_note: The job_run_timeline shape (period_start_time/period_end_time slicing, result_state populated only on an end row) is the same one lakeflow_job_run_cost and lakeflow_job_reliability already confirm against a live workspace, and this query's own result_state/termination_code/run_start reads follow lakeflow_job_run_cost's own verified end-row convention (see caveats). The per-run cost join (usage_metadata.job_id/job_run_id, DEC-66.1) is lakeflow_job_run_cost's own, unchanged. sql_error_sample (query_source.job_info.job_run_id, the de-value pattern) is new and has not itself been run against a live account - confirm it returns a plausible error shape for a run you know failed on a SQL task.
-- read_this: One row = one job run, capped to each job's :runs_per_job most recent runs by run_start. This is a drill-down, not a finding - no status/WARN/CRITICAL band. The columns that matter are run_minutes, result_state/termination_code (the run's final outcome), attempts (how many times it was tried, repairs included), failing_task_keys (which task inside the run actually failed, when known), net_dbus/net_list_cost (the run's own discounted-at-list cost) and sql_error_sample (the de-identified error text of a failed SQL task, when the run had one).
-- healthy: n/a - inventory (a run-history drill-down for the Jobs run panel, not a scored check)
-- investigate_if: n/a - inventory; result_state IN ('FAILED','ERROR','TIMED_OUT') is a failed run - read failing_task_keys, sql_error_sample and termination_code for the cause
-- actions: n/a - inventory (reference/drill-down input)
-- next: lakeflow_failed_runs (the account-wide failed-run rollup this feeds), lakeflow_job_run_cost (the account-wide per-run cost rollup this reuses), lakeflow_retries_repairs (if a run kept retrying)
-- caveats: Grain is one row per (workspace_id, job_id, run_id); job_id is unique only within a workspace, so every join here is on (workspace_id, job_id). ranked, capped to :runs_per_job most recent runs BY run_start per job - a job with more runs in the window than :runs_per_job shows only its newest ones; raise :runs_per_job if you need deeper history. run_start is the run's TRUE first observed slice - MIN(period_start_time) across every row of the run_id, whichever attempt it belongs to (a run over ~1h is sliced hourly, only the end row carries result_state). RESULT/TERMINATION/ATTEMPTS - unlike lakeflow_job_reliability's own daily aggregate, there is no incomplete-current-day cutoff here: a run whose only end row landed today is still a real, finished run, not "still running" (result_state IS NOT NULL keeps end rows only). result_state/termination_code come from the run's LAST end row only (highest period_end_time), never MAX(result_state)/MAX(termination_code) - comparing attempts' text alphabetically would pair an earlier attempt's TIMED_OUT ahead of a later SUCCEEDED; a repaired run shows only its final outcome, not "TIMED_OUT -- SUCCEEDED". attempts is the count of end rows for the run_id (a repaired run took more than one). in_flight is true (and run_end/result_state/termination_code/attempts are NULL) when the run has no end row of its own yet in this snapshot at all - genuinely still running; run_minutes then covers only the time observed so far, through the current moment, not the run's eventual total - run_duration_is_lower_bound is true whenever run_duration_seconds itself was never populated on the run's own end row (read before that column existed, or the run is still in_flight), so run_minutes was computed from wall-clock period timestamps instead and may run short if a slice's own period_end_time understates true end time. failing_task_keys/failing_task_termination_code come from system.lakeflow.job_task_run_timeline end rows in FAILED/ERROR/TIMED_OUT for the SAME run_id in the window; both are NULL when the run did not fail, or when it failed but no task-level breakdown exists for it yet (a submit/workflow run, or a gap before task-level timeline was populated) - NULL here is never proof the run succeeded, only that no failing task was found. COST - net_dbus/net_list_cost are this run's own share, from the exact per-run attribution and effective-list price join lakeflow_job_run_cost uses (usage_metadata.job_id/job_run_id, DEC-66.1, usage_unit matched), priced over the SAME :period_days window; NULL (never 0) when no billed usage row carries this run's ids - an hourly or otherwise very short job can genuinely show NULL here if its billing has not landed or its DBUs rounded to nothing. sql_error_sample is the de-valued (emails and string literals stripped, same pattern query_failed_queries_daily uses) error text of this run's FAILED system.query.history statements (query_source.job_info.job_run_id = this run_id); NULL for a run with no SQL task, a run that did not fail on SQL, or one whose statements have not landed yet. job_name comes from system.lakeflow.jobs (SCD2, latest row by change_time, deleted jobs kept) and is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions, which never write to that table. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (one line) and resolve ids with cost_workspace_names separately. No identities are emitted (sql_error_sample is de-valued at source), so nothing here needs masking.
WITH all_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported   -- 0 = not reported on a multi-task job
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
),
run_span AS (
  -- the run's TRUE first observed slice, across every attempt's own rows (see caveats)
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM all_rows
  GROUP BY workspace_id, job_id, run_id
),
end_rows AS (
  -- no day cutoff here (unlike lakeflow_job_reliability's own daily aggregate): this is a
  -- per-run listing, so a run whose only end row landed today is still a real, finished run --
  -- dropping it would read as "still running" (see caveats).
  SELECT workspace_id, job_id, run_id, period_end_time, result_state, termination_code, run_s_reported
  FROM all_rows
  WHERE result_state IS NOT NULL
),
run_last_end AS (
  -- the run's LAST attempt's own end row only - never MAX(result_state)/MAX(termination_code),
  -- which compares attempts' text alphabetically (see caveats)
  SELECT workspace_id, job_id, run_id, result_state, termination_code, run_s_reported,
         period_end_time                                                    AS run_end,
         COUNT(*) OVER (PARTITION BY workspace_id, job_id, run_id)          AS attempts
  FROM end_rows
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC
  ) = 1
),
run_obs AS (
  SELECT s.workspace_id, s.job_id, s.run_id, s.run_start,
         e.run_end, e.result_state, e.termination_code, e.run_s_reported, e.attempts,
         (e.run_id IS NULL) AS in_flight,
         COALESCE(e.run_s_reported,
                  timestampdiff(SECOND, s.run_start,
                                CASE WHEN e.run_id IS NULL THEN current_timestamp() ELSE e.run_end END)) AS run_s
  FROM run_span s
  LEFT JOIN run_last_end e
    ON  e.workspace_id = s.workspace_id AND e.job_id = s.job_id AND e.run_id = s.run_id
),
failing_tasks AS (
  SELECT workspace_id, job_id, job_run_id,
         concat_ws(', ', collect_set(task_key)) AS failing_task_keys,
         MAX(termination_code)                  AS failing_task_termination_code
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  GROUP BY workspace_id, job_id, job_run_id
),
run_cost AS (
  -- the exact per-run attribution and effective-list price join lakeflow_job_run_cost uses
  -- (usage_metadata.job_id/job_run_id, DEC-66.1, usage_unit matched)
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                AS net_dbus,
         SUM(u.usage_quantity * lp.list_rate) AS net_list_cost
  FROM system.billing.usage u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
    FROM system.billing.list_prices
  ) lp
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
    AND u.usage_date < current_date()
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
sql_errors AS (
  -- de-valued (emails / string literals stripped) error text of this run's own failed SQL
  -- statements, same de-value pattern query_failed_queries_daily uses
  SELECT workspace_id,
         query_source.job_info.job_run_id AS job_run_id,
         MAX(
           regexp_replace(
             regexp_replace(error_message, '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}', '<email>'),
             concat(chr(39), '[^', chr(39), ']*', chr(39)), '?'
           )
         ) AS sql_error_sample
  FROM system.query.history
  WHERE start_time >= dateadd(day, -:period_days, current_date())
    AND execution_status = 'FAILED'
    AND query_source.job_info.job_run_id IS NOT NULL
  GROUP BY workspace_id, query_source.job_info.job_run_id
),
latest_jobs AS (
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id ORDER BY change_time DESC) = 1
),
ranked AS (
  SELECT r.*,
         ROW_NUMBER() OVER (PARTITION BY r.workspace_id, r.job_id ORDER BY r.run_start DESC) AS run_rank
  FROM run_obs r
)
SELECT r.workspace_id,
       w.workspace_name,
       r.job_id,
       j.job_name,                                                  -- NULL for submit/workflow runs
       r.run_id,
       r.run_start,
       CASE WHEN r.in_flight THEN NULL ELSE r.run_end END           AS run_end,
       r.in_flight,
       ROUND(r.run_s / 60.0, 1)                                     AS run_minutes,
       (r.run_s_reported IS NULL)                                   AS run_duration_is_lower_bound,
       r.result_state,
       r.termination_code,
       r.attempts,
       ft.failing_task_keys,
       ft.failing_task_termination_code,
       ROUND(rc.net_dbus, 2)                                        AS net_dbus,
       ROUND(rc.net_list_cost, 2)                                   AS net_list_cost,
       se.sql_error_sample
FROM ranked r
LEFT JOIN failing_tasks ft
  ON r.workspace_id = ft.workspace_id AND r.job_id = ft.job_id AND r.run_id = ft.job_run_id
LEFT JOIN run_cost rc
  ON r.workspace_id = rc.workspace_id AND r.job_id = rc.job_id AND r.run_id = rc.job_run_id
LEFT JOIN sql_errors se
  ON r.workspace_id = se.workspace_id AND r.run_id = se.job_run_id
LEFT JOIN latest_jobs j ON r.workspace_id = j.workspace_id AND r.job_id = j.job_id
LEFT JOIN system.access.workspaces_latest w ON r.workspace_id = w.workspace_id
WHERE r.run_rank <= :runs_per_job
ORDER BY r.workspace_id, r.job_id, r.run_start DESC
