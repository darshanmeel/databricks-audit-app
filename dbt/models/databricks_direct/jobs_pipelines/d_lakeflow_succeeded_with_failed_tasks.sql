{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_succeeded_with_failed_tasks.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH job_end AS (
  SELECT workspace_id, job_id, run_id AS job_run_id, result_state AS job_result_state
  FROM {{ source('system_lakeflow', 'job_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    AND result_state IS NOT NULL
),
task_end AS (
  SELECT workspace_id, job_id, job_run_id, task_key, result_state AS task_result_state
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND result_state IS NOT NULL
)
SELECT j.workspace_id, j.job_id,
       COUNT(DISTINCT j.job_run_id) AS succeeded_runs,
       COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT')
                           THEN j.job_run_id END) AS succeeded_runs_with_failed_task,
       -- status: worst-first band on succeeded-but-had-a-failed-task run count (field heuristic;
       -- {{ param('lakeflow_succeeded_with_failed_tasks', 'warn_succeeded_with_failed', 3) }} / {{ param('lakeflow_succeeded_with_failed_tasks', 'crit_succeeded_with_failed', 10) }}).
       CASE
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= {{ param('lakeflow_succeeded_with_failed_tasks', 'crit_succeeded_with_failed', 10) }} THEN 'CRITICAL'
         WHEN COUNT(DISTINCT CASE WHEN t.task_result_state IN ('FAILED','ERROR','TIMED_OUT') THEN j.job_run_id END) >= {{ param('lakeflow_succeeded_with_failed_tasks', 'warn_succeeded_with_failed', 3) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM job_end j
LEFT JOIN task_end t
  ON j.workspace_id = t.workspace_id AND j.job_id = t.job_id AND j.job_run_id = t.job_run_id
WHERE j.job_result_state = 'SUCCEEDED'
GROUP BY j.workspace_id, j.job_id
ORDER BY succeeded_runs_with_failed_task DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
