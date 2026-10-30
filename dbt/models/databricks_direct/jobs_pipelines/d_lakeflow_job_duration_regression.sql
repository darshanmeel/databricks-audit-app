{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_duration_regression.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH all_rows AS (
  -- every row, not just end rows: a run over ~1h is sliced hourly and only its LAST row carries
  -- result_state (see caveats), so the attempt's true start has to come from its own earlier
  -- NULL-state slices too, not from the last slice alone.
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time, result_state,
         NULLIF(run_duration_seconds, 0)   AS run_s_reported,
         NULLIF(queue_duration_seconds, 0) AS queue_s_reported,
         NULLIF(setup_duration_seconds, 0) AS setup_s_reported,
         NULLIF(execution_duration_seconds, 0) AS execution_s_reported
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})   -- drop incomplete current day
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
  -- a non-empty remainder to compare against, even when {{ param('lakeflow_job_duration_regression', 'recent_days', 7) }} >= {{ w }} (the
  -- account's shortest offered window); see caveats.
  SELECT workspace_id, job_id, run_id, final_result_state, attempt_rows, duration_s,
         queue_s_reported, setup_s_reported, execution_s_reported,
         CASE WHEN run_start >= dateadd(day, -LEAST({{ param('lakeflow_job_duration_regression', 'recent_days', 7) }}, CAST(FLOOR({{ w }} / 2) AS INT)), {{ audit_today() }})
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
  FROM {{ source('system_lakeflow', 'jobs') }}
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
         WHEN a.recent_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }}
           OR a.baseline_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }}                                            THEN 'NOT_ASSESSED'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= {{ param('lakeflow_job_duration_regression', 'crit_slowdown_ratio', 2.0) }}       THEN 'CRITICAL'
         WHEN (a.recent_median_s / NULLIF(a.baseline_median_s, 0)) >= {{ param('lakeflow_job_duration_regression', 'warn_slowdown_ratio', 1.5) }}        THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.recent_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }} AND a.baseline_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }} THEN 'too_few_runs_both_sides'
         WHEN a.recent_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }}                                           THEN 'too_few_recent_runs'
         WHEN a.baseline_runs < {{ param('lakeflow_job_duration_regression', 'min_runs_each_side', 3) }}                                         THEN 'too_few_baseline_runs'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
LEFT JOIN grown g       ON g.workspace_id = a.workspace_id AND g.job_id = a.job_id
LEFT JOIN latest_jobs j ON j.workspace_id = a.workspace_id AND j.job_id = a.job_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         slowdown_ratio DESC,
         workspace_id, job_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
