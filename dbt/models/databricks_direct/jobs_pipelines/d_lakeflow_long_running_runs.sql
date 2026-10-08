{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:standard', 'drilldown', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_long_running_runs.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
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
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  GROUP BY workspace_id, job_id, run_id
),
run_obs AS (
  SELECT workspace_id, job_id, run_id, run_start, last_seen, result_state, termination_code,
         run_s_reported, setup_s, queue_s, execution_s, cleanup_s,
         (result_state IS NULL) AS in_flight,
         -- reported duration when the column is there, else wall clock (a lower bound)
         COALESCE(run_s_reported,
                  timestampdiff(SECOND, run_start,
                                CASE WHEN result_state IS NULL THEN {{ audit_now() }}
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
                                CASE WHEN MAX(result_state) IS NULL THEN {{ audit_now() }}
                                     ELSE MAX(period_end_time) END)) AS task_s
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
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
  FROM {{ source('system_lakeflow', 'jobs') }}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
),
ws AS (
  -- workspace name + the base URL the deep links are built from (the stored URL ends in '/')
  SELECT workspace_id, workspace_name,
         regexp_replace(workspace_url, '/+$', '') AS base_url
  FROM {{ source('system_access', 'workspaces_latest') }}
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
       -- {{ param('lakeflow_long_running_runs', 'warn_run_hours', 2) }} / {{ param('lakeflow_long_running_runs', 'crit_run_hours', 6) }}).
       CASE
         WHEN r.run_s IS NULL                      THEN 'NOT_ASSESSED'
         WHEN r.run_s >= {{ param('lakeflow_long_running_runs', 'crit_run_hours', 6) }} * 3600    THEN 'CRITICAL'
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
WHERE r.run_s >= {{ param('lakeflow_long_running_runs', 'warn_run_hours', 2) }} * 3600
   OR r.run_s IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         run_hours DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
