{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:performance', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_per_query_estimate_lane.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT date_trunc('HOUR', start_time) AS usage_hour, workspace_id,
       compute.warehouse_id AS warehouse_id, compute.type AS compute_type,
       {{ mask_user('executed_by', 'executed_by_user_id') }} AS executed_by,
       statement_id, statement_type,
       execution_duration_ms, waiting_for_compute_duration_ms, total_task_duration_ms,
       read_bytes, total_duration_ms
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
  AND execution_status = 'FINISHED'
  AND from_result_cache = false
  AND execution_duration_ms > 0
  AND compute.warehouse_id IS NOT NULL
ORDER BY usage_hour, warehouse_id, statement_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
