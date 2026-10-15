-- generated from dbt/models/databricks_direct/storage/d_po_vacuum_reclaimed_bytes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_vacuum_reclaimed_bytes.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       COUNT(*) AS vacuum_op_count,
       SUM(CAST(operation_metrics['number_of_deleted_files']      AS BIGINT))  AS total_deleted_files,
       SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT))  AS total_deleted_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS vacuum_estimated_dbu,
       -- status: worst-first band on DBU spent with zero bytes reclaimed (field heuristic; 5 / 20).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 20 THEN 'CRITICAL'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'VACUUM' AND operation_status = 'SUCCESSFUL'
  AND start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AND start_time < __AS_OF_DATE__
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, vacuum_estimated_dbu DESC
) q
