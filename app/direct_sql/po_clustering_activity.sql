-- generated from dbt/models/databricks_direct/storage/d_po_clustering_activity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_clustering_activity.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name, operation_type,
       COUNT(*) AS op_count,
       SUM(CAST(operation_metrics['number_of_removed_files']        AS BIGINT)) AS removed_files,
       SUM(CAST(operation_metrics['number_of_clustered_files']      AS BIGINT)) AS clustered_files,
       SUM(CAST(operation_metrics['amount_of_data_removed_bytes']   AS BIGINT)) AS removed_bytes,
       SUM(CAST(operation_metrics['amount_of_clustered_data_bytes'] AS BIGINT)) AS clustered_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS clustering_estimated_dbu,
       -- status: worst-first band on clustering DBU spend per table (field heuristic; 50 / 200).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 200 THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'CLUSTERING'
  AND start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AND start_time < __AS_OF_DATE__
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_type
ORDER BY clustering_estimated_dbu DESC
) q
