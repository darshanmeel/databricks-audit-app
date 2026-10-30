{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_termination_type_probe.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, job_id, termination_type, COUNT(*) AS run_rows
FROM {{ source('system_lakeflow', 'job_run_timeline') }}
WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND result_state IS NOT NULL
GROUP BY workspace_id, job_id, termination_type
ORDER BY workspace_id, run_rows DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
