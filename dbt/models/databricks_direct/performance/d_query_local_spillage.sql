{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_local_spillage.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
       {{ mask_user('executed_by', 'MAX(executed_by_user_id)') }} AS executed_by,
       COUNT(*) AS spilling_query_count,
       SUM(spilled_local_bytes) AS spilled_local_bytes_sum,
       MAX(spilled_local_bytes) AS spilled_local_bytes_max,
       SUM(total_duration_ms)     AS total_duration_ms_sum,
       SUM(execution_duration_ms) AS execution_duration_ms_sum,
       SUM(shuffle_read_bytes)    AS shuffle_read_bytes_sum,
       -- status: worst-first band on daily local spill (field heuristic; {{ param('query_local_spillage', 'warn_spill_gb', 1) }} / {{ param('query_local_spillage', 'crit_spill_gb', 10) }}).
       CASE
         WHEN SUM(spilled_local_bytes) >= {{ param('query_local_spillage', 'crit_spill_gb', 10) }} * 1e9 THEN 'CRITICAL'
         WHEN SUM(spilled_local_bytes) >= {{ param('query_local_spillage', 'warn_spill_gb', 1) }} * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
  AND spilled_local_bytes > 0
GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id, executed_by
ORDER BY spilled_local_bytes_sum DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
