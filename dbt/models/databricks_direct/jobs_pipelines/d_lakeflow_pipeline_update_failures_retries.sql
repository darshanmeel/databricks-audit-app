{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:jobs_pipelines', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/jobs_pipelines/lakeflow_pipeline_update_failures_retries.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH end_rows AS (
  SELECT workspace_id, pipeline_id, update_id, request_id, update_type,
         trigger_type, result_state, period_start_time, period_end_time
  FROM {{ source('system_lakeflow', 'pipeline_update_timeline') }}
  WHERE period_start_time >= dateadd(day, -{{ w }}, {{ audit_today() }})
    AND period_end_time < date_trunc('DAY', {{ audit_now() }})
    AND result_state IS NOT NULL          -- end row only for updates >1h
)
SELECT e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state,
       COUNT(DISTINCT e.update_id) AS updates,
       SUM(CASE WHEN e.result_state = 'FAILED'             THEN 1 ELSE 0 END) AS failed_update_rows,
       SUM(CASE WHEN e.trigger_type = 'RETRY_ON_FAILURE'   THEN 1 ELSE 0 END) AS retry_triggered_rows,
       -- status: worst-first band on failed update rows per group (field heuristic; {{ param('lakeflow_pipeline_update_failures_retries', 'warn_failed_updates', 3) }} / {{ param('lakeflow_pipeline_update_failures_retries', 'crit_failed_updates', 10) }}).
       CASE
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= {{ param('lakeflow_pipeline_update_failures_retries', 'crit_failed_updates', 10) }} THEN 'CRITICAL'
         WHEN SUM(CASE WHEN e.result_state = 'FAILED' THEN 1 ELSE 0 END) >= {{ param('lakeflow_pipeline_update_failures_retries', 'warn_failed_updates', 3) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM end_rows e
GROUP BY e.workspace_id, e.pipeline_id, e.update_type, e.trigger_type, e.result_state
ORDER BY failed_update_rows DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
