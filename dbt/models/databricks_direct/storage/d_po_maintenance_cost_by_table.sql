{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/storage/po_maintenance_cost_by_table.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
       operation_type, operation_status, usage_unit,
       COUNT(*)                                  AS operation_count,
       SUM(CAST(usage_quantity AS DECIMAL(38,6))) AS estimated_dbu,
       MIN(start_time) AS first_op_time, MAX(end_time) AS last_op_time,
       -- status: worst-first band; any FAILED row is CRITICAL, else banded on estimated_dbu (field heuristic; {{ param('po_maintenance_cost_by_table', 'warn_maint_dbu', 20) }} / {{ param('po_maintenance_cost_by_table', 'crit_maint_dbu', 100) }}).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_status LIKE 'FAILED%' THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_maintenance_cost_by_table', 'crit_maint_dbu', 100) }} THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_maintenance_cost_by_table', 'warn_maint_dbu', 20) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_storage', 'predictive_optimization_operations_history') }}
WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
  AND start_time < {{ audit_today() }}
GROUP BY account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
         operation_type, operation_status, usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, estimated_dbu DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
