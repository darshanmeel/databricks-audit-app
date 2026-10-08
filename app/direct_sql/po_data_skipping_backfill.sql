-- generated from dbt/models/databricks_direct/storage/d_po_data_skipping_backfill.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_data_skipping_backfill.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['added_data_skipping_columns']   AS added_data_skipping_columns,
       operation_metrics['removed_data_skipping_columns'] AS removed_data_skipping_columns,
       operation_metrics['new_data_skipping_columns']     AS new_data_skipping_columns,
       SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) AS scanned_bytes,
       SUM(CAST(operation_metrics['number_of_scanned_files'] AS BIGINT)) AS scanned_files,
       MAX(end_time) AS last_event_time,
       -- status: worst-first band on scan cost with zero ADDED-column gain (field heuristic; 100 / 500).
       -- new_data_skipping_columns is not a reliable gain signal (real events don't populate it) - see caveats.
       CASE
         WHEN SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['added_data_skipping_columns'] IS NULL
              AND operation_metrics['removed_data_skipping_columns'] IS NULL
              AND operation_metrics['new_data_skipping_columns'] IS NULL THEN 'NOT_ASSESSED'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= 500 * 1e9 THEN 'CRITICAL'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= 100 * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'DATA_SKIPPING_COLUMN_SELECTION'
  AND start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AND start_time < __AS_OF_DATE__
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['added_data_skipping_columns'], operation_metrics['removed_data_skipping_columns'],
         operation_metrics['new_data_skipping_columns']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, scanned_bytes DESC
) q
