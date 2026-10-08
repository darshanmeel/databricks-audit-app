-- generated from dbt/models/databricks_direct/storage/d_po_clustering_column_churn.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/po_clustering_column_churn.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['has_column_selection_changed'] AS has_column_selection_changed,
       operation_metrics['old_clustering_columns']        AS old_clustering_columns,
       operation_metrics['new_clustering_columns']        AS new_clustering_columns,
       operation_metrics['additional_reason']             AS additional_reason,
       MAX(end_time) AS last_selection_time,
       COUNT(*)      AS selection_event_count,
       -- status: worst-first band on repeated true churn events per table (field heuristic; 2 / 5).
       CASE
         WHEN operation_metrics['has_column_selection_changed'] IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND COUNT(*) >= 5 THEN 'CRITICAL'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND COUNT(*) >= 2 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`storage`.`predictive_optimization_operations_history`
WHERE operation_type = 'AUTO_CLUSTERING_COLUMN_SELECTION'
  AND start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AND start_time < __AS_OF_DATE__
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['has_column_selection_changed'], operation_metrics['old_clustering_columns'],
         operation_metrics['new_clustering_columns'], operation_metrics['additional_reason']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, selection_event_count DESC
) q
