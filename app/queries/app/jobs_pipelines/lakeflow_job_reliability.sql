-- query_id: lakeflow_job_reliability
-- title: Per job: failure rate over the window and whether it is failing right now
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.jobs
-- requires: SELECT on system.lakeflow; GA (system.lakeflow.job_run_timeline and
--   system.lakeflow.jobs are generally available)
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :last_n_runs (default 10) how many of
--   the job's most recent runs make up the "last N runs" summary; :min_runs (default 5) minimum
--   distinct runs in the window before a failure rate is judged (below -> NOT_ASSESSED, unless the
--   CRITICAL streak rule already fired); :warn_failure_rate_pct (default 20) failure rate percent
--   (0-100) at/above which WARN, when there are at least :min_runs runs; :crit_consecutive_failures
--   (default 3) consecutive failed runs, counting back from the very latest run, at/above which
--   CRITICAL regardless of :min_runs
-- confidence: needs_confirmation
-- confidence_note: Built on the exact end-row and attempt-row conventions lakeflow_failed_runs and
--   lakeflow_retries_repairs already verified against a live workspace (period_end_time <
--   date_trunc('DAY', now) drops the incomplete current day, result_state IS NOT NULL keeps end
--   rows only, multiple non-NULL-result_state rows sharing one run_id are repair/retry attempts);
--   the failed-result_state set (FAILED, ERROR, TIMED_OUT) is lakeflow_failed_runs's own verified
--   filter. The consecutive-failure streak and the last-N-runs summary are new derived logic on top
--   of those verified primitives and have not themselves been run against a live workspace.
-- read_this: One row = one job with at least one completed run (an end row) in the window. runs is
--   the job's distinct run count - a retried run counts once, on its FINAL attempt's outcome. The
--   columns that matter are consecutive_failures (is the job broken right now), failure_rate_pct
--   (is the job flaky over the whole window), and last_n_summary (a plain-words readout of the same
--   last :last_n_runs runs last_n_runs_considered/last_n_failed give as numbers). status folds all
--   three into one band; read investigate_if for the precedence. suggested_timeout_minutes is a
--   concrete timeout to set (2x the job's own p95 successful-run duration, never below its longest
--   successful run), NULL until the job has 5+ successful runs to measure it from.
-- healthy: status = OK - at least :min_runs runs in the window, failure_rate_pct below
--   :warn_failure_rate_pct, and the job is not on a failure streak at/above
--   :crit_consecutive_failures; fewer runs than that reads NOT_ASSESSED (too_few_runs), never OK.
-- investigate_if: CRITICAL - consecutive_failures at/above :crit_consecutive_failures: the job's
--   most recent runs failed back-to-back and it is very likely broken right now - read
--   latest_termination_code first. WARN - failure_rate_pct at/above :warn_failure_rate_pct with at
--   least :min_runs runs: the job is flaky rather than currently broken. NOT_ASSESSED - fewer than
--   :min_runs runs in the window and no live failure streak: read not_assessed_reason.
-- actions: 1) open the latest failed run and read its own error, not an older one -
--   latest_termination_code names the code (free); 2) fix the root cause, or tune the job's
--   retry/backoff policy so a transient failure stops repeating - runs_with_retry shows how often a
--   repair was already needed (config); 3) if the failures trace back to under-provisioned or
--   misconfigured compute, resize or reconfigure the job cluster (spend).
-- next: lakeflow_failed_runs (for the account-wide termination_code breakdown),
--   lakeflow_retries_repairs (for the DBU cost of the repairs this query only counts),
--   lakeflow_termination_taxonomy (for the full termination_code picture),
--   lakeflow_job_duration_regression (a job can be reliable and still be getting slower)
-- not_assessed_reasons: too_few_runs: too few runs in the window to judge reliability
-- caveats: End-row and window handling is identical to lakeflow_failed_runs / lakeflow_retries_repairs:
--   period_end_time < date_trunc('DAY', current_timestamp()) drops the incomplete current day, and
--   result_state IS NOT NULL keeps end rows only. A run retried or repaired writes one end row PER
--   ATTEMPT sharing run_id (the same visibility caveat lakeflow_retries_repairs documents); this
--   query dedupes every run_id to its LAST attempt (latest period_end_time), so a repaired run
--   counts once, on its FINAL outcome - a run that failed then was repaired and succeeded reads as
--   one SUCCEEDED run, never as one FAILED and one SUCCEEDED, and attempt_rows > 1 on that run is
--   what makes it count toward runs_with_retry. Failed is the same three-value set
--   lakeflow_failed_runs uses (FAILED, ERROR, TIMED_OUT); SKIPPED / CANCELLED / BLOCKED are not
--   counted as failed for failure_rate_pct - a deliberately skipped or cancelled run is not, on
--   its own, a reliability signal - but see consecutive_failures below for how the streak treats
--   one sitting between two real failures.
--   run_start (used for both the last-N-runs summary and the consecutive-failure streak) is the
--   run's own TRUE first observed slice - MIN(period_start_time) across every row of the run_id,
--   not the end row's own period_start_time alone (job_run_timeline slices a run over
--   ~1h hourly and only the end row carries result_state, so reading the end row alone reported the
--   start of its LAST slice, not the run's true beginning - a run over ~4h showed the wrong-by-hours
--   latest_run_start). consecutive_failures counts back from the LATEST run (recency rank 1) and stops ONLY at
--   the job's most recent SUCCEEDED run (or, if it has none in the window, counts every failed run
--   there is) - a SKIPPED, CANCELLED or BLOCKED run in between is stepped over, never treated as
--   if it broke the streak: FAILED, FAILED, FAILED, CANCELLED (oldest to newest) still reads
--   consecutive_failures = 3, because someone killing (or a policy skipping) a hung, already-
--   failing job is not the same thing as it recovering. It is computed over every run in the
--   window, independent of :last_n_runs and independent of :min_runs - a job with only 3 runs, all
--   3 failed, and the default
--   :crit_consecutive_failures = 3 reads CRITICAL even though 3 is below the default :min_runs = 5,
--   because "the job is broken right now" is the more urgent fact (status precedence: CRITICAL is
--   checked first, regardless of :min_runs; then NOT_ASSESSED if runs < :min_runs; then WARN; else
--   OK - NOT_ASSESSED never means the job is fine). last_n_runs_considered / last_n_failed /
--   last_n_summary summarise the job's most recent :last_n_runs runs (fewer when the job has fewer
--   runs than that in the window) - distinct from failure_rate_pct, which is over EVERY run in the
--   window, so a job whose failures are all old and whose most recent runs are clean can show a
--   non-trivial failure_rate_pct alongside "0 of last N runs failed". job_id is unique only within a
--   workspace, so every grouping is on (workspace_id, job_id). job_name comes from
--   system.lakeflow.jobs (SCD2, latest row by change_time, deleted jobs kept) and is NULL for
--   one-time SUBMIT_RUN / WORKFLOW_RUN executions, which never write to that table.
--   latest_result_state / latest_termination_code describe the single latest run's own final
--   attempt, for context next to consecutive_failures. successful_runs counts every SUCCEEDED run,
--   repaired or not; p95_success_minutes / max_success_minutes are narrower - SUCCEEDED runs with
--   exactly one attempt only, since a repaired run's duration_s spans from its FIRST (failed)
--   attempt's start to its final attempt's end, and folding that in would inflate the timeout
--   suggestion by however long the earlier failed attempt(s) ran. suggested_timeout_minutes = CEIL(GREATEST(2 x p95_success_minutes, max_success_minutes))
--   once successful_runs >= 5, else NULL - never a number invented from too few runs; it never
--   reads below the longest successful run actually observed. There are no dollars here - a failure
--   is not priced; price a failing job with lakeflow_failed_jobs_wasted_dbus.
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported   -- 0 = not reported on a multi-task job
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
),
run_first AS (
  -- the run's TRUE first observed slice, not just its end row's own slice start -- a run over ~1h
  -- is sliced hourly and only the end row carries result_state.
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_final AS (
  -- one row per RUN, deduplicated to its FINAL attempt: a repaired/retried run writes one
  -- non-NULL-result_state end row per attempt, sharing run_id (see caveats) - the run's own
  -- outcome is the outcome of its LAST attempt, and attempt_rows counts every attempt it took.
  SELECT e.workspace_id, e.job_id, e.run_id,
         f.run_start,
         e.period_end_time                                           AS run_end,
         e.run_s_reported,
         e.result_state                                              AS final_result_state,
         e.termination_code                                          AS final_termination_code,
         COUNT(*) OVER (PARTITION BY e.workspace_id, e.job_id, e.run_id) AS attempt_rows
  FROM end_rows e
  JOIN run_first f
    ON  f.workspace_id = e.workspace_id AND f.job_id = e.job_id AND f.run_id = e.run_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.workspace_id, e.job_id, e.run_id ORDER BY e.period_end_time DESC
  ) = 1
),
run_judged AS (
  SELECT workspace_id, job_id, run_id, run_start, final_result_state, final_termination_code,
         attempt_rows,
         CASE WHEN final_result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN 1 ELSE 0 END AS is_failed,
         CASE WHEN attempt_rows > 1 THEN 1 ELSE 0 END                                       AS repaired,
         -- reported duration when the column is there, else wall clock from the run's true start
         -- (T1: feeds suggested_timeout_minutes below)
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, run_end)) AS duration_s,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, job_id ORDER BY run_start DESC, run_id DESC
         )                                                                                  AS recency_rank
  FROM run_final
),
run_first_success AS (
  -- the recency_rank of the job's most recent SUCCEEDED run (NULL if it has none in the window)
  -- -- the only rank that stops the streak; a SKIPPED / CANCELLED / BLOCKED run in between is
  -- stepped over, never treated as if it broke the streak (see caveats).
  SELECT workspace_id, job_id, run_id, recency_rank, is_failed,
         MIN(CASE WHEN final_result_state = 'SUCCEEDED' THEN recency_rank END) OVER (
           PARTITION BY workspace_id, job_id
         ) AS first_success_rank
  FROM run_judged
),
job_streak AS (
  -- consecutive failures counting back from the latest run: every FAILED/ERROR/TIMED_OUT run
  -- more recent than the job's most recent SUCCEEDED run (or, if it has none in the window,
  -- every failed run there is) -- a SKIPPED/CANCELLED/BLOCKED run in between does not count
  -- toward the streak, but does not stop it either.
  SELECT workspace_id, job_id,
         SUM(CASE WHEN is_failed = 1 AND recency_rank < COALESCE(first_success_rank, 2147483647)
                  THEN 1 ELSE 0 END) AS consecutive_failures
  FROM run_first_success
  GROUP BY workspace_id, job_id
),
job_totals AS (
  SELECT workspace_id, job_id,
         COUNT(*)         AS runs,
         SUM(is_failed)   AS failed_runs,
         SUM(repaired)    AS runs_with_retry,
         MAX(run_start)   AS latest_run_start
  FROM run_judged
  GROUP BY workspace_id, job_id
),
last_n AS (
  SELECT workspace_id, job_id,
         COUNT(*)       AS last_n_runs_considered,
         SUM(is_failed) AS last_n_failed
  FROM run_judged
  WHERE recency_rank <= :last_n_runs
  GROUP BY workspace_id, job_id
),
success_stats AS (
  -- T1: a concrete timeout suggestion needs 5+ successful runs to mean anything (see caveats).
  SELECT workspace_id, job_id,
         SUM(CASE WHEN final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS successful_runs,
         percentile(CASE WHEN final_result_state = 'SUCCEEDED' AND attempt_rows = 1 THEN duration_s / 60.0 END, 0.95) AS p95_success_minutes,
         MAX(CASE WHEN final_result_state = 'SUCCEEDED' AND attempt_rows = 1 THEN duration_s / 60.0 END)              AS max_success_minutes
  FROM run_judged
  GROUP BY workspace_id, job_id
),
latest AS (
  SELECT workspace_id, job_id,
         final_result_state     AS latest_result_state,
         final_termination_code AS latest_termination_code
  FROM run_judged
  WHERE recency_rank = 1
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT t.workspace_id,
       t.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       t.runs,
       t.failed_runs,
       ROUND(100.0 * t.failed_runs / NULLIF(t.runs, 0), 1)         AS failure_rate_pct,
       t.runs_with_retry,
       n.last_n_runs_considered,
       n.last_n_failed,
       CONCAT(CAST(n.last_n_failed AS STRING), ' of last ',
              CAST(n.last_n_runs_considered AS STRING), ' runs failed') AS last_n_summary,
       s.consecutive_failures,
       t.latest_run_start,
       l.latest_result_state,
       l.latest_termination_code,
       ss.successful_runs,
       ROUND(ss.p95_success_minutes, 1) AS p95_success_minutes,
       ROUND(ss.max_success_minutes, 1) AS max_success_minutes,
       -- T1: a concrete change, never invented from too few runs (caveats) - 2x p95, never below
       -- the longest successful run, only once there are 5+ successful runs to measure it from.
       CASE WHEN ss.successful_runs >= 5
            THEN CEIL(GREATEST(2 * ss.p95_success_minutes, ss.max_success_minutes))
            ELSE NULL END AS suggested_timeout_minutes,
       -- status: a live failure streak wins first, regardless of :min_runs (field heuristic).
       CASE
         WHEN s.consecutive_failures >= :crit_consecutive_failures            THEN 'CRITICAL'
         WHEN t.runs < :min_runs                                             THEN 'NOT_ASSESSED'
         WHEN (100.0 * t.failed_runs / NULLIF(t.runs, 0)) >= :warn_failure_rate_pct THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.consecutive_failures >= :crit_consecutive_failures THEN NULL
         WHEN t.runs < :min_runs                                   THEN 'too_few_runs'
         ELSE NULL
       END AS not_assessed_reason
FROM job_totals t
JOIN job_streak s ON s.workspace_id = t.workspace_id AND s.job_id = t.job_id
JOIN last_n n     ON n.workspace_id = t.workspace_id AND n.job_id = t.job_id
JOIN latest l     ON l.workspace_id = t.workspace_id AND l.job_id = t.job_id
LEFT JOIN success_stats ss ON ss.workspace_id = t.workspace_id AND ss.job_id = t.job_id
LEFT JOIN latest_jobs j ON j.workspace_id = t.workspace_id AND j.job_id = t.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         s.consecutive_failures DESC,
         failure_rate_pct DESC,
         workspace_id, job_id
