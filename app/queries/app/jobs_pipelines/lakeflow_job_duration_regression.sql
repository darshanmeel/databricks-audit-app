-- query_id: lakeflow_job_duration_regression
-- title: Per job: how its recent run duration compares to its own earlier baseline in the same window
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.jobs
-- requires: SELECT on system.lakeflow; GA (system.lakeflow.job_run_timeline and
--   system.lakeflow.jobs are generally available)
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :recent_days (default 7) the most
--   recent N days of the window is the job's "recent" slice, capped at HALF of :period_days (see
--   caveats) so the earlier remainder always has a non-empty "baseline" to compare against;
--   :min_runs_each_side (default 3) minimum SUCCESSFUL runs required on EACH side (recent and
--   baseline) before a ratio is judged, below -> NOT_ASSESSED; :warn_slowdown_ratio (default 1.5)
--   recent median duration over baseline median duration at/above which WARN; :crit_slowdown_ratio
--   (default 2.0) ratio at/above which CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Built on the same end-row, attempt-row and wall-clock-fallback conventions
--   lakeflow_retries_repairs and lakeflow_long_running_runs already verified against a live
--   workspace (period_end_time < date_trunc('DAY', now) drops the incomplete current day,
--   result_state IS NOT NULL marks an end row, NULLIF(run_duration_seconds, 0) treats the
--   documented multi-task 0-sentinel as not-reported and falls back to timestampdiff wall clock).
--   Pooling a multi-hour attempt's own earlier NULL-result_state slices (a run over ~1h is sliced
--   hourly, per the doc lakeflow_long_running_runs already verified this against) into that same
--   attempt's start, the recent-vs-baseline split, and measuring a repaired run on its final
--   attempt's own wall clock rather than the calendar gap before a repair, are new logic on top of
--   those verified primitives and have not themselves been run against a live workspace.
-- read_this: One row = one job with at least one completed run (an end row) in the window. Only
--   SUCCEEDED runs feed a duration - a run that ultimately failed tells you nothing about how long
--   the job normally takes. recent_median_minutes is the median wall-clock duration of the job's
--   SUCCEEDED runs in the last :recent_days days; baseline_median_minutes is the SAME job's own
--   median over the earlier, remaining days of the window; slowdown_ratio is recent over baseline.
--   recent_runs/baseline_runs are the SUCCEEDED-run counts each median is built on - read these
--   before trusting a ratio built on a handful of runs - and the _repaired columns say how many of
--   those needed at least one retry. recent_queue_s/setup_s/execution_s and their baseline
--   counterparts are the same two medians broken into the three legacy phase columns (NULL where
--   Databricks does not report them - see caveats); grew_most says which side actually grew -
--   'queue_or_setup' points at capacity (a busier cluster pool, slower autoscale), 'run_time' at
--   the job's own code or its input data.
-- healthy: status = OK - slowdown_ratio below :warn_slowdown_ratio (this includes a job that got
--   FASTER: ratio < 1) with enough successful runs judged on both sides.
-- investigate_if: CRITICAL - slowdown_ratio at/above :crit_slowdown_ratio: the job's recent runs
--   are taking meaningfully longer than its own earlier runs in the same window. WARN - at/above
--   :warn_slowdown_ratio. NOT_ASSESSED - fewer than :min_runs_each_side successful runs on the
--   recent side, the baseline side, or both: read not_assessed_reason; comparing two measured
--   periods is fine, but not on too few runs to mean anything.
-- actions: 1) open the job's recent runs and compare input volume against an older one - a bigger
--   input is the single most common cause (free); 2) if the input did not change, check whether the
--   job's cluster changed (a smaller node type, fewer workers, a different pool) -
--   lakeflow_job_compute_pressure names memory/CPU/skew pressure on the same job (config); 3) if the
--   job is genuinely processing more data than before, scale the job cluster up or out to match
--   (spend).
-- next: lakeflow_job_reliability (a job can be slow and still succeed every time, or be both slow
--   and flaky), lakeflow_job_compute_pressure (for why - memory, CPU, skew), lakeflow_long_running_runs
--   (for the individual runs behind a slow median), lakeflow_retries_repairs (a repair adds attempts,
--   not duration, to this query's own numbers - but still costs DBUs)
-- not_assessed_reasons: too_few_runs_both_sides: too few successful runs on both the recent and
--   the earlier side of the window to compare; too_few_recent_runs: too few successful recent
--   runs to compare; too_few_baseline_runs: too few successful earlier runs to compare against
-- caveats: Only runs whose FINAL attempt is SUCCEEDED contribute a duration - a run that failed
--   outright, or that a repair never rescued, is excluded entirely from both medians, the same
--   "successful runs only" choice lakeflow_long_running_runs's own job-median baseline makes.
--   SLICING - system.lakeflow.job_run_timeline splits a run past roughly an hour into hourly
--   rows, and only the LAST row of an attempt carries result_state (lakeflow_long_running_runs
--   documents this too); this query therefore pools an attempt's OWN rows first (every row from
--   after the previous attempt's end, or the run's own start, up to and including this attempt's
--   end row) into one true wall-clock span before measuring it, rather than reading only the
--   final row's own short start-to-end - a run that grew from 2h to 5h now reads as 5h, not as
--   its own last <=1h slice. A repaired/retried run (more than one attempt sharing run_id, the
--   same visibility caveat lakeflow_retries_repairs documents) is measured on its LAST attempt's
--   OWN pooled wall clock (that attempt's own true start to its own end row), never the calendar
--   time from the run's first attempt to its last - an earlier failed attempt's duration, and the
--   human time before someone clicked repair, would otherwise inflate "how long the job took"
--   with time the job itself was not running; this is the opposite choice from
--   lakeflow_long_running_runs, which pools EVERY attempt row of a run_id into one MIN/MAX wall
--   clock and is measuring "how long did this run_id occupy", a different question. A run whose
--   final attempt's own first slice started before :period_days ago (so the window bound in
--   all_rows drops it) is measured short - keep :period_days comfortably wider than the longest
--   run you expect, the same advice lakeflow_long_running_runs gives. run_duration_seconds is
--   read the same way lakeflow_long_running_runs reads it: NULLIF(run_duration_seconds, 0)
--   (Databricks documents it, like its four duration siblings, as populated only for legacy
--   single-task jobs and logged as a literal 0 - never NULL - for every multi-task job, and only
--   on an attempt's own end row), falling back to timestampdiff(SECOND, ...) of that same pooled
--   attempt span when the column reads 0 or the job is multi-task; both medians are therefore a
--   lower bound wherever that fallback was used. RECENT VS BASELINE - the recent slice is
--   period_start_time >= (today - LEAST(:recent_days, FLOOR(:period_days / 2))); the baseline
--   slice is the rest of the window. The FLOOR cap means the recent slice is never more than half
--   the chosen window, so the baseline side is never empty by construction - at the app's
--   shortest offered window (7 days, where :recent_days's own default of 7 would otherwise cover
--   the whole thing), the effective split is 3 recent days against 4 baseline days; at 30 or 90
--   it stays the full 7-day default recent slice, since 7 is already <= half of those windows. A
--   job whose ACTUAL runs are all newer than the baseline cutoff (a job that simply has not been
--   running long) still, correctly, reads NOT_ASSESSED (too_few_baseline_runs) - the cap fixes
--   the split being empty by construction, not a genuinely short history. A run is bucketed by
--   its OWN final attempt's pooled start. DEC-62 is why the split works this way at all - both
--   medians are measured inside the window the user picked, never one projected or scaled from
--   the other. job_id is unique only within a workspace, so grouping is on (workspace_id,
--   job_id). job_name comes from system.lakeflow.jobs (SCD2, latest row, deleted jobs kept) and
--   is NULL for one-time SUBMIT_RUN / WORKFLOW_RUN executions. On a partial snapshot the baseline
--   side (the older half of the window) is the one most likely to be thin - the generic
--   window_coverage marker (DEC-64) is the account-wide signal to check first. There are no
--   dollars here - durations are not a billing unit; a job that is also costing more is
--   lakeflow_failed_jobs_wasted_dbus / lakeflow_retries_repairs's question, not this one's.
--   PHASE COLUMNS (T12) - queue_duration_seconds/setup_duration_seconds/execution_duration_seconds
--   are, per Databricks' own documentation, populated only for legacy single-task jobs and log a
--   literal 0 (never NULL) on every multi-task job's run-level row, on the run's own end row only -
--   read the same way run_duration_seconds itself is (NULLIF(..., 0), "0 = not reported"), pooled
--   across a repaired run's own attempts the same way duration_s is. A multi-task job therefore
--   reads every one of these six columns and grew_most as NULL, never a false 0. grew_most compares
--   the RECENT-over-BASELINE ratio of (queue+setup) against that of execution alone (each phase
--   COALESCEd to 0 before summing, so one missing phase does not null out the other's own real
--   reading) - it names a side only when that side's own ratio is above 1 (it actually grew) AND
--   is the larger of the two; NULL when neither side grew, or neither is measurable.
WITH all_rows AS (
  -- every row, not just end rows: a run over ~1h is sliced hourly and only its LAST row carries
  -- result_state (see caveats), so the attempt's true start has to come from its own earlier
  -- NULL-state slices too, not from the last slice alone.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0)   AS run_s_reported,
         NULLIF(queue_duration_seconds, 0) AS queue_s_reported,
         NULLIF(setup_duration_seconds, 0) AS setup_s_reported,
         NULLIF(execution_duration_seconds, 0) AS execution_s_reported
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
    AND period_end_time < date_trunc('DAY', current_timestamp())   -- drop incomplete current day
),
row_marked AS (
  -- prev_attempt_end: the END time of this run_id's most recent PRIOR attempt (NULL for a run's
  -- first, or only, attempt). Constant across every row of one attempt, including its own
  -- earlier NULL-state hourly slices, because it only advances once an attempt's own end row has
  -- been passed - this is what lets attempt_span below collapse a whole attempt, sliced hourly or
  -- not, down to its own true start.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         run_s_reported, queue_s_reported, setup_s_reported, execution_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) OVER (
           PARTITION BY workspace_id, job_id, run_id
           ORDER BY period_start_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ) AS prev_attempt_end
  FROM all_rows
),
attempt_span AS (
  -- one row per ATTEMPT (not per run, not per hourly slice): every row sharing a run_id AND a
  -- prev_attempt_end is part of the same attempt, so this is the fix for the hourly-slicing bug -
  -- attempt_start is the attempt's own true first minute, not its last slice's start.
  SELECT workspace_id, job_id, run_id, prev_attempt_end,
         MIN(period_start_time)                                          AS attempt_start,
         MAX(CASE WHEN result_state IS NOT NULL THEN period_end_time END) AS attempt_end,
         MAX(CASE WHEN result_state IS NOT NULL THEN result_state END)    AS attempt_result_state,
         MAX(CASE WHEN result_state IS NOT NULL THEN run_s_reported END)  AS run_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN queue_s_reported END)     AS queue_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN setup_s_reported END)     AS setup_s_reported,
         MAX(CASE WHEN result_state IS NOT NULL THEN execution_s_reported END) AS execution_s_reported
  FROM row_marked
  GROUP BY workspace_id, job_id, run_id, prev_attempt_end
),
run_final AS (
  -- one row per RUN, deduplicated to its FINAL attempt (see lakeflow_job_reliability and
  -- caveats above): a repaired run's duration is measured across every slice of its LAST attempt,
  -- from that attempt's own true start to its own end. An attempt still in flight right now
  -- (attempt_end IS NULL, no end row observed yet) is dropped - this query only measures a run
  -- once it has finished.
  SELECT workspace_id, job_id, run_id,
         attempt_start                                              AS run_start,
         attempt_end                                                AS run_end,
         attempt_result_state                                       AS final_result_state,
         run_s_reported, queue_s_reported, setup_s_reported, execution_s_reported,
         COUNT(*) OVER (PARTITION BY workspace_id, job_id, run_id)  AS attempt_rows
  FROM attempt_span
  WHERE attempt_end IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id, run_id ORDER BY attempt_end DESC
  ) = 1
),
run_duration AS (
  SELECT workspace_id, job_id, run_id, run_start, final_result_state, attempt_rows,
         -- reported duration when the column is there (legacy single-task job), else wall clock
         -- of this same final attempt, now spanning every slice it took (a lower bound only when
         -- the attempt's own first slice fell before the window start; see caveats)
         COALESCE(run_s_reported, timestampdiff(SECOND, run_start, run_end)) AS duration_s,
         -- queue/setup/execution stay NULL when not reported (legacy-single-task-only columns,
         -- T12) - never a wall-clock fallback, since there is no wall-clock split of the run into
         -- these three phases without them
         queue_s_reported, setup_s_reported, execution_s_reported
  FROM run_final
),
bucketed AS (
  -- the "recent" slice is capped at half the chosen window (FLOOR, matching Spark's truncating
  -- integer division so both dialects agree) so it is always a real sub-slice of the window with
  -- a non-empty remainder to compare against, even when :recent_days >= :period_days (the
  -- account's shortest offered window); see caveats.
  SELECT workspace_id, job_id, run_id, final_result_state, attempt_rows, duration_s,
         queue_s_reported, setup_s_reported, execution_s_reported,
         CASE WHEN run_start >= dateadd(day, -LEAST(:recent_days, CAST(FLOOR(:period_days / 2) AS INT)), current_date())
              THEN 'RECENT' ELSE 'BASELINE' END AS bucket
  FROM run_duration
),
agg AS (
  SELECT workspace_id, job_id,
         SUM(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS recent_runs,
         SUM(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED'
                   AND attempt_rows > 1                                        THEN 1 ELSE 0 END) AS recent_runs_repaired,
         SUM(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS baseline_runs,
         SUM(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED'
                   AND attempt_rows > 1                                        THEN 1 ELSE 0 END) AS baseline_runs_repaired,
         percentile(
           CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN duration_s END, 0.5
         ) AS recent_median_s,
         percentile(
           CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN duration_s END, 0.5
         ) AS baseline_median_s,
         -- T12: which part grew - capacity (queue/setup) or the code/data itself (execution)
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN queue_s_reported END, 0.5) AS recent_queue_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN queue_s_reported END, 0.5) AS baseline_queue_s,
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN setup_s_reported END, 0.5) AS recent_setup_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN setup_s_reported END, 0.5) AS baseline_setup_s,
         percentile(CASE WHEN bucket = 'RECENT'   AND final_result_state = 'SUCCEEDED' THEN execution_s_reported END, 0.5) AS recent_execution_s,
         percentile(CASE WHEN bucket = 'BASELINE' AND final_result_state = 'SUCCEEDED' THEN execution_s_reported END, 0.5) AS baseline_execution_s
  FROM bucketed
  GROUP BY workspace_id, job_id
),
grown AS (
  -- T12: which part grew more, proportionally - queue+setup (capacity) or execution (code/data).
  -- A side's ratio is NULL, not a false 0/1, whenever either its own baseline or recent median
  -- could not be measured (see caveats) - never invented from a missing reading.
  SELECT workspace_id, job_id,
         (COALESCE(recent_queue_s, 0) + COALESCE(recent_setup_s, 0))
           / NULLIF(COALESCE(baseline_queue_s, 0) + COALESCE(baseline_setup_s, 0), 0) AS qs_ratio,
         recent_execution_s / NULLIF(baseline_execution_s, 0)                       AS exec_ratio
  FROM agg
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT a.workspace_id,
       a.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       a.recent_runs,
       a.recent_runs_repaired,
       a.baseline_runs,
       a.baseline_runs_repaired,
       ROUND(a.recent_median_s / 60.0, 1)                          AS recent_median_minutes,
       ROUND(a.baseline_median_s / 60.0, 1)                        AS baseline_median_minutes,
       ROUND(a.recent_median_s / NULLIF(a.baseline_median_s, 0), 2) AS slowdown_ratio,
       a.recent_queue_s, a.baseline_queue_s,
       a.recent_setup_s, a.baseline_setup_s,
       a.recent_execution_s, a.baseline_execution_s,
       -- T12: names a side only when it actually grew (ratio > 1) and grew more than the other
       -- measurable side - NULL when neither grew, or neither is measurable (see caveats)
       CASE
         WHEN g.qs_ratio > 1 AND (g.exec_ratio IS NULL OR g.qs_ratio >= g.exec_ratio)   THEN 'queue_or_setup'
         WHEN g.exec_ratio > 1 AND (g.qs_ratio IS NULL OR g.exec_ratio > g.qs_ratio)     THEN 'run_time'
         ELSE NULL
       END AS grew_most,
       CASE
         WHEN a.recent_runs < :min_runs_each_side
           OR a.baseline_runs < :min_runs_each_side                                            THEN 'NOT_ASSESSED'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= :crit_slowdown_ratio       THEN 'CRITICAL'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= :warn_slowdown_ratio        THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.recent_runs < :min_runs_each_side AND a.baseline_runs < :min_runs_each_side THEN 'too_few_runs_both_sides'
         WHEN a.recent_runs < :min_runs_each_side                                           THEN 'too_few_recent_runs'
         WHEN a.baseline_runs < :min_runs_each_side                                         THEN 'too_few_baseline_runs'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
LEFT JOIN grown g       ON g.workspace_id = a.workspace_id AND g.job_id = a.job_id
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         slowdown_ratio DESC,
         workspace_id, job_id
