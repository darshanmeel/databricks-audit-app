{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/storage/po_clustering_activity.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name, operation_type,
       COUNT(*) AS op_count,
       SUM(CAST(operation_metrics['number_of_removed_files']        AS BIGINT)) AS removed_files,
       SUM(CAST(operation_metrics['number_of_clustered_files']      AS BIGINT)) AS clustered_files,
       SUM(CAST(operation_metrics['amount_of_data_removed_bytes']   AS BIGINT)) AS removed_bytes,
       SUM(CAST(operation_metrics['amount_of_clustered_data_bytes'] AS BIGINT)) AS clustered_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS clustering_estimated_dbu,
       -- status: worst-first band on clustering DBU spend per table (field heuristic; {{ param('po_clustering_activity', 'warn_clustering_dbu', 50) }} / {{ param('po_clustering_activity', 'crit_clustering_dbu', 200) }}).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_clustering_activity', 'crit_clustering_dbu', 200) }} THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= {{ param('po_clustering_activity', 'warn_clustering_dbu', 50) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_storage', 'predictive_optimization_operations_history') }}
WHERE operation_type = 'CLUSTERING'
  AND start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS AND start_time < {{ audit_today() }}
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_type
ORDER BY clustering_estimated_dbu DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
