{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:jobs_pipelines', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/jobs_pipelines/lakeflow_failed_cluster_starts.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH final_tasks AS (
  SELECT workspace_id, job_id, period_start_time, period_end_time, result_state,
         COALESCE(NULLIF(termination_code, ''), 'unknown') AS termination_code,
         setup_duration_seconds, execution_duration_seconds
  FROM {{ source('system_lakeflow', 'job_task_run_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    AND result_state IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, job_id, run_id ORDER BY period_end_time DESC) = 1
)
SELECT workspace_id,
       job_id,
       termination_code,
       COUNT(*)                    AS failed_starts,
       SUM(setup_duration_seconds) AS setup_s_total,
       MIN(period_start_time)      AS first_failed_at,
       MAX(period_end_time)        AS last_failed_at
FROM final_tasks
WHERE result_state IN ('FAILED', 'ERROR', 'TIMED_OUT')
  AND setup_duration_seconds IS NOT NULL
  AND COALESCE(execution_duration_seconds, 0) = 0
GROUP BY workspace_id, job_id, termination_code
ORDER BY failed_starts DESC, setup_s_total DESC, workspace_id, job_id, termination_code
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
