{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_workload_mix_hours.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, hour(start_time) AS hour_of_day,
       compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       statement_type,
       {{ mask_user('executed_by', 'MAX(executed_by_user_id)') }} AS executed_by,
       COUNT(*) AS query_count,
       SUM(CASE WHEN execution_status = 'FINISHED' THEN 1 ELSE 0 END) AS finished_count,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(read_bytes)            AS read_bytes_sum,
       SUM(produced_rows)         AS produced_rows_sum
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
GROUP BY date(start_time), workspace_id, hour(start_time), compute.type, compute.warehouse_id, statement_type, executed_by
ORDER BY day, hour_of_day, workspace_id, compute_type, warehouse_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
