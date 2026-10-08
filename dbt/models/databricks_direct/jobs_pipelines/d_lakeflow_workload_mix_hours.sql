{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, job_id, run_type, trigger_type,
       date_trunc('DAY', period_start_time) AS run_day,
       COUNT(DISTINCT run_id) AS distinct_runs,
       SUM(CASE WHEN result_state IS NOT NULL THEN 1 ELSE 0 END) AS completed_run_rows,
       SUM(execution_duration_seconds) AS execution_s_total
FROM {{ source('system_lakeflow', 'job_run_timeline') }}
WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND period_start_time < date_trunc('DAY', {{ audit_now() }})
GROUP BY workspace_id, job_id, run_type, trigger_type, date_trunc('DAY', period_start_time)
ORDER BY workspace_id, run_day DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
