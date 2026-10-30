{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/storage/po_vacuum_reclaimed_bytes.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       COUNT(*) AS vacuum_op_count,
       SUM(CAST(operation_metrics['number_of_deleted_files']      AS BIGINT))  AS total_deleted_files,
       SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT))  AS total_deleted_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS vacuum_estimated_dbu,
       -- status: worst-first band on DBU spent with zero bytes reclaimed (field heuristic; {{ param('po_vacuum_reclaimed_bytes', 'warn_noop_dbu', 5) }} / {{ param('po_vacuum_reclaimed_bytes', 'crit_noop_dbu', 20) }}).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_vacuum_reclaimed_bytes', 'crit_noop_dbu', 20) }} THEN 'CRITICAL'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_vacuum_reclaimed_bytes', 'warn_noop_dbu', 5) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_storage', 'predictive_optimization_operations_history') }}
WHERE operation_type = 'VACUUM' AND operation_status = 'SUCCESSFUL'
  AND start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS AND start_time < {{ audit_today() }}
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, vacuum_estimated_dbu DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
