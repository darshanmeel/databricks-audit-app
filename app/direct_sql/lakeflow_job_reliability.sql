-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_reliability.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_reliability.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, job_id, run_id, period_start_time, period_end_time,
         result_state, termination_code,
         NULLIF(run_duration_seconds, 0) AS run_s_reported   -- 0 = not reported on a multi-task job
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_end_time < date_trunc('DAY', __AS_OF_TS__)   -- drop incomplete current day
    AND result_state IS NOT NULL                                   -- end row only
),
run_first AS (
  -- the run's TRUE first observed slice, not just its end row's own slice start -- a run over ~1h
  -- is sliced hourly and only the end row carries result_state.
  SELECT workspace_id, job_id, run_id, MIN(period_start_time) AS run_start
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
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
  WHERE recency_rank <= 10
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
  FROM `system`.`lakeflow`.`jobs`
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
       -- status: a live failure streak wins first, regardless of 5 (field heuristic).
       CASE
         WHEN s.consecutive_failures >= 3            THEN 'CRITICAL'
         WHEN t.runs < 5                                             THEN 'NOT_ASSESSED'
         WHEN (100.0 * t.failed_runs / NULLIF(t.runs, 0)) >= 20 THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.consecutive_failures >= 3 THEN NULL
         WHEN t.runs < 5                                   THEN 'too_few_runs'
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
) q
