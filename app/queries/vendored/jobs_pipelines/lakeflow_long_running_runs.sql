-- query_id: lakeflow_long_running_runs
-- title: Job runs past a duration bound, and the task eating the time
-- domain: jobs_pipelines   tier: standard
-- reads: system.lakeflow.job_run_timeline, system.lakeflow.job_task_run_timeline, system.lakeflow.jobs, system.access.workspaces_latest
-- requires: SELECT on system.lakeflow AND on system.access (the latter only for the workspace_name column - see caveats to drop it); GA (the five *_duration_seconds columns were added early Dec 2025)
-- empty_if: schema_not_enabled, no_activity, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_run_hours (default 2) run wall-clock hours at/above which a run is reported and flagged WARN; :crit_run_hours (default 6) run wall-clock hours that flag CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Checked against the Databricks doc pages on 2026-09-21 (not against a live workspace): the doc states that run_duration_seconds and the four phase columns on job_run_timeline are populated only for legacy single-task jobs and log 0 for every multi-task job, so this query now treats 0 as "not reported" and falls back to wall clock - without that, no multi-task run could ever reach :warn_run_hours; the doc also states that SKIPPED / BLOCKED tasks do write a zero-length timeline row. The run/task join key and the SCD2 name lookup follow the three verified queries in this folder (lakeflow_succeeded_with_failed_tasks, lakeflow_tasks_near_timeout, lakeflow_phase_cold_start). Still unverified on a workspace: 1) the in-flight path (result_state IS NULL measured against current_timestamp()) - confirm an in-flight run shows in_flight = true with a run_hours close to the Jobs UI; 2) try_element_at(compute_ids, 1) returns a cluster id that top_task_cluster_url opens (the doc says compute_ids also carries SQL warehouse ids, for which the cluster link is meaningless); 3) workspace_name / job_name resolve, with job_name NULL only for submit / workflow runs.
-- read_this: One row = one job run that lasted at least :warn_run_hours (plus any run whose duration could not be established). The columns that matter are run_hours (wall clock), top_task_key with top_task_share_pct (which task ate the run), and x_vs_job_p50 (this run against the same job's median run in the window). job_name and top_3_tasks are there so a row reads without a second lookup, and run_url / top_task_cluster_url open the run and its compute straight from the result grid.
-- healthy: no rows - no run reached :warn_run_hours in the window - field heuristic; tune :warn_run_hours for your account, it is the whole point of this query.
-- investigate_if: any row - run_hours at/above :warn_run_hours (WARN), at/above :crit_run_hours or still in flight past the bound (CRITICAL) - field heuristic. Read x_vs_job_p50 next: well above 1 means this run is abnormal for this job (look at that run), near 1 means the job is always this slow (look at the job's design). wait_share_pct above ~30 means the time went to setup/queue, not to work (NULL for every multi-task job - the phase columns are legacy-only, see caveats - lakeflow_phase_cold_start is the job-level p95 view of that same setup/queue breakdown, RUN-level like this query, not task-level; it now folds the same multi-task 0-sentinel to NULL that this query already falls back to wall clock for).
-- actions: 1) open the flagged run's top_task_key and split the time: wait_share_pct high means cluster start or queueing, otherwise compare the task's input volume against a normal run of the same job (free); 2) give the job and its tasks a timeout so a runaway is killed instead of billing until someone notices, and cut the dominant task down - narrow the window it reprocesses, split it, or run independent tasks in parallel instead of in sequence (config); 3) if the dominant task is genuinely compute-bound, move it to a warm pool or serverless to kill the cold start, or give it a faster node type / more workers (spend).
-- next: lakeflow_phase_cold_start (if wait_share_pct is high - the per-job setup/queue breakdown), lakeflow_tasks_near_timeout (if the dominant task is closing on its configured timeout), lakeflow_jobs_no_timeout (if the long run had no timeout to stop it), lakeflow_retries_repairs (if the run is long because it kept retrying), cost_workspace_names (if you dropped the workspaces_latest join and want to resolve workspace_id separately)
-- caveats: This query deliberately does NOT drop the current day the way the other duration queries in this folder do, because a run that is over the bound right now is the case you most want to catch; the cost is that very recent rows may still be materializing, so a run that started minutes ago can be missing or measured short. run_duration_seconds and the four phase columns are not populated before early Dec 2025, are never populated while a run is in flight, and - per the doc - are populated only for legacy single-task jobs while every multi-task job logs 0 in them, so a 0 is treated as not reported and this falls back to wall clock between the run's first and last observed rows; run_s_is_lower_bound marks every row where that fallback was used (for a multi-task job, every row), and wait_share_pct is NULL rather than a false 0 on those rows. A run that started BEFORE the window is only visible through its in-window rows, so its measured duration is a lower bound too - keep :period_days comfortably wider than :warn_run_hours. result_state is populated only in a run's end row, so in_flight = true means "no end row seen yet", which is also what ingestion lag looks like. top_3_tasks lists the three slowest task_keys of the run with their observed hours, longest first, so the shape of a run reads without a second query; a run with fewer than three observed tasks simply lists fewer. top_task_key is the task with the most observed seconds (execution_duration_seconds where populated, else wall clock across that task's rows); tasks run in parallel, so the task seconds of a run can add up to more than the run's own seconds and top_task_share_pct can exceed 100 - read it as "the share of the run's wall clock this task was busy", not as a decomposition of the run. A task that never ran (SKIPPED, BLOCKED) writes a single zero-length row per the doc and a task still waiting writes none, so neither can be a top task and a run whose time is spent waiting on a dependency shows a small top_task_share_pct - that gap is the finding. x_vs_job_p50 uses the same window, and the long runs themselves are in that median, so it is conservative: a job that is ALWAYS slow sits near 1.0, which is a chronic-slowness signal rather than a false negative. Names are resolved by LEFT JOIN so a missing name never drops a row: job_name comes from system.lakeflow.jobs, which is SCD2 (the latest row per job by change_time is taken, deleted jobs included, so a long run of a since-deleted job keeps its last known name) and which one-time SUBMIT_RUN / WORKFLOW_RUN executions never write to - those runs show job_name NULL and are identified by job_id alone. workspace_name comes from system.access.workspaces_latest, a DIFFERENT system schema: if system.access is not enabled or you cannot read it, this query ERRORS rather than degrading, so drop the workspace_name column and its LEFT JOIN (two lines) and resolve ids with cost_workspace_names separately - that is why the cost domain keeps the lookup separate. No user or principal identities are emitted, so nothing here needs masking; task names are task_keys, which are job configuration, not identities. The three URLs are built from workspaces_latest.workspace_url (its trailing slash is stripped), so they share the system.access dependency above and are NULL without it. Databricks documents a per-run page but NO per-task deep link, so run_url is the task entry point: it opens the run's task graph, from which a task's output and Spark UI are one click away. top_task_cluster_id is the FIRST entry of that task's compute_ids array, so a task that moved across compute shows only the first; it is NULL for serverless tasks (no cluster exists) and on rows from before compute_ids was populated (early Dec 2025) - a NULL link there means "no cluster recorded", never "no cluster used". There are no dollars here - durations are not a billing unit; price a long run with lakeflow_failed_jobs_wasted_dbus or the cost domain.
WITH run_agg AS (
  SELECT workspace_id, job_id, run_id,
         MIN(period_start_time)          AS run_start,
         MAX(period_end_time)            AS last_seen,
         MAX(result_state)               AS result_state,      -- NULL until the run's end row lands
         MAX(termination_code)           AS termination_code,
         -- end row only; NULL before Dec 2025 and 0 for every multi-task job (doc), so 0 = not reported
         NULLIF(MAX(run_duration_seconds), 0)       AS run_s_reported,
         NULLIF(MAX(setup_duration_seconds), 0)     AS setup_s,
         NULLIF(MAX(queue_duration_seconds), 0)     AS queue_s,
         NULLIF(MAX(execution_duration_seconds), 0) AS execution_s,
         NULLIF(MAX(cleanup_duration_seconds), 0)   AS cleanup_s
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id, run_start, last_seen, result_state, termination_code,
         run_s_reported, setup_s, queue_s, execution_s, cleanup_s,
         (result_state IS NULL) AS in_flight,
         -- reported duration when the column is there, else wall clock (a lower bound)
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN current_timestamp()
                                     ELSE last_seen END)) AS run_s
  FROM run_agg
),
task_obs AS (
  SELECT workspace_id, job_id, job_run_id, task_key,
         MAX(result_state) AS task_result_state,
         -- first compute id of the task; NULL on serverless and on pre-Dec-2025 rows
         MAX(try_element_at(compute_ids, 1)) AS cluster_id,
         COALESCE(NULLIF(MAX(execution_duration_seconds), 0),
                  timestampdiff(SECOND, MIN(period_start_time),
                                CASE WHEN MAX(result_state) IS NULL THEN current_timestamp()
                                     ELSE MAX(period_end_time) END)) AS task_s
  FROM system.lakeflow.job_task_run_timeline
  WHERE period_start_time >= dateadd(day, -:period_days, current_date())
  GROUP BY workspace_id, job_id, job_run_id, task_key
),
task_ranked AS (
  SELECT workspace_id, job_id, job_run_id, task_key, task_result_state, task_s, cluster_id,
         ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, job_run_id
                            ORDER BY task_s DESC, task_key) AS task_rank
  FROM task_obs
),
task_top AS (
  SELECT workspace_id, job_id, job_run_id,
         COUNT(*)                                                   AS tasks_seen,
         SUM(CASE WHEN task_result_state IS NULL THEN 1 ELSE 0 END) AS tasks_in_flight,
         MAX(CASE WHEN task_rank = 1 THEN task_key END)             AS top_task_key,
         MAX(CASE WHEN task_rank = 1
                  THEN COALESCE(task_result_state, 'RUNNING') END)  AS top_task_state,
         MAX(CASE WHEN task_rank = 1 THEN task_s END)               AS top_task_s,
         MAX(CASE WHEN task_rank = 1 THEN cluster_id END)           AS top_task_cluster_id,
         -- the three slowest tasks by name, longest first; CONCAT_WS drops the missing ranks
         CONCAT_WS(', ',
           MAX(CASE WHEN task_rank = 1 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END),
           MAX(CASE WHEN task_rank = 2 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END),
           MAX(CASE WHEN task_rank = 3 THEN CONCAT(task_key, '=', ROUND(task_s / 3600.0, 2), 'h') END)
         )                                                          AS top_3_tasks
  FROM task_ranked
  GROUP BY workspace_id, job_id, job_run_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  -- Deleted jobs are kept (a long run of a since-deleted job still deserves its name).
  SELECT workspace_id, job_id, name AS job_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM system.access.workspaces_latest
),
job_norm AS (
  -- the same job's own normal, from the finished runs in the window
  SELECT workspace_id, job_id,
         COUNT(*)                          AS finished_runs_in_window,
         percentile(run_s, 0.5)     AS run_s_p50
  FROM run_obs
  WHERE result_state IS NOT NULL
  GROUP BY workspace_id, job_id
)
SELECT r.workspace_id,
       w.workspace_name,                                         -- needs system.access; see caveats
       r.job_id,
       j.job_name,                                               -- NULL for submit/workflow runs
       r.run_id,
       r.run_start,
       CASE WHEN r.in_flight THEN NULL ELSE r.last_seen END      AS run_end,
       r.in_flight,
       ROUND(r.run_s / 3600.0, 2)                                AS run_hours,
       (r.run_s_reported IS NULL)                                AS run_s_is_lower_bound,
       r.setup_s, r.queue_s, r.execution_s, r.cleanup_s,
       -- how much of the run was waiting to start rather than working; NULL when the phase
       -- columns are not reported (every multi-task job - see caveats), never a false 0
       CASE WHEN r.setup_s IS NULL AND r.queue_s IS NULL THEN NULL
            ELSE ROUND(100.0 * (COALESCE(r.setup_s, 0) + COALESCE(r.queue_s, 0))
                       / NULLIF(r.run_s, 0), 1) END              AS wait_share_pct,
       t.tasks_seen, t.tasks_in_flight,
       t.top_task_key, t.top_task_state,
       t.top_3_tasks,
       ROUND(t.top_task_s / 3600.0, 2)                           AS top_task_hours,
       ROUND(100.0 * t.top_task_s / NULLIF(r.run_s, 0), 1)       AS top_task_share_pct,
       ROUND(n.run_s_p50 / 3600.0, 2)                            AS job_p50_hours,
       ROUND(r.run_s / NULLIF(n.run_s_p50, 0), 1)                AS x_vs_job_p50,
       n.finished_runs_in_window,
       r.result_state, r.termination_code,
       -- deep links; all three are NULL without system.access (see caveats)
       CONCAT(w.base_url, '/jobs/', r.job_id)                     AS job_url,
       CONCAT(w.base_url, '/jobs/', r.job_id, '/runs/', r.run_id) AS run_url,
       t.top_task_cluster_id,
       CONCAT(w.base_url, '/compute/clusters/',
              t.top_task_cluster_id)                              AS top_task_cluster_url,
       -- status: worst-first band on wall-clock run hours, with a still-running run past the
       -- bound treated as CRITICAL because it is the one you can still stop (field heuristic;
       -- :warn_run_hours / :crit_run_hours).
       CASE
         WHEN r.run_s IS NULL                      THEN 'NOT_ASSESSED'
         WHEN r.run_s >= :crit_run_hours * 3600    THEN 'CRITICAL'
         WHEN r.in_flight                          THEN 'CRITICAL'
         ELSE 'WARN'
       END AS status
FROM run_obs r
LEFT JOIN task_top t
  ON  r.workspace_id = t.workspace_id
  AND r.job_id       = t.job_id
  AND r.run_id       = t.job_run_id
LEFT JOIN job_norm n
  ON  r.workspace_id = n.workspace_id
  AND r.job_id       = n.job_id
LEFT JOIN latest_jobs j
  ON  r.workspace_id = j.workspace_id
  AND r.job_id       = j.job_id
LEFT JOIN ws w
  ON  r.workspace_id = w.workspace_id
-- keep the un-measurable runs instead of dropping them: they surface as NOT_ASSESSED
WHERE r.run_s >= :warn_run_hours * 3600
   OR r.run_s IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         run_hours DESC
