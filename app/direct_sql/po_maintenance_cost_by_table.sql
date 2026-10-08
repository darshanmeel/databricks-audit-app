-- generated from dbt/models/databricks_direct/storage/d_po_maintenance_cost_by_table.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_maintenance_cost_by_table.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
       operation_type, operation_status, usage_unit,
       COUNT(*)                                  AS operation_count,
       SUM(CAST(usage_quantity AS DECIMAL(38,6))) AS estimated_dbu,
       MIN(start_time) AS first_op_time, MAX(end_time) AS last_op_time,
       -- status: worst-first band; any FAILED row is CRITICAL, else banded on estimated_dbu (field heuristic; 20 / 100).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_status LIKE 'FAILED%' THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 100 THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= 20 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
  AND start_time < __AS_OF_DATE__
GROUP BY account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
         operation_type, operation_status, usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, estimated_dbu DESC
) q
