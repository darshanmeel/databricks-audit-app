{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/performance/perf_queue_by_hour.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT date(start_time) AS usage_date,
       hour(start_time) AS usage_hour,
       concat(workspace_id, ':', compute.warehouse_id) AS warehouse_key,
       workspace_id,
       compute.warehouse_id AS warehouse_id,
       COUNT(*) AS runs,
       SUM(CASE WHEN COALESCE(waiting_at_capacity_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS queued_runs,
       ROUND(SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0, 1) AS slot_wait_s,
       SUM(CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS provision_runs,
       ROUND(SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0, 1) AS provision_s
FROM {{ source('system_query', 'history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
  AND compute.warehouse_id IS NOT NULL
GROUP BY date(start_time), hour(start_time), workspace_id, compute.warehouse_id
ORDER BY usage_date DESC, usage_hour, warehouse_key
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
