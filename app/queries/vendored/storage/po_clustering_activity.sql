-- query_id: po_clustering_activity
-- title: Liquid clustering activity and DBU cost by table
-- domain: storage   tier: standard
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_operations_history), regional
-- empty_if: po_not_enabled, schema_not_enabled, preview_unavailable, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_clustering_dbu (default 50) estimated clustering DBUs per table over the window that flags WARN; :crit_clustering_dbu (default 200) estimated clustering DBUs per table over the window that flags CRITICAL
-- confidence: confirmed
-- confidence_note: Column names and the four CLUSTERING operation_metrics subfields queried here are verified against Databricks system-table docs.
-- read_this: One row = a table's incremental liquid-clustering (CLUSTERING) activity over the window, split by workspace_id (the workspace whose compute ran the operation). The column that matters is clustering_estimated_dbu - the DBUs Predictive Optimization spent compacting/re-clustering that table in that workspace. Some clustering activity is normal; a table burning outsized DBUs on it repeatedly is the signal worth chasing.
-- healthy: clustering_estimated_dbu below :warn_clustering_dbu DBUs per table over the window (field heuristic - tune :warn_clustering_dbu for your account).
-- investigate_if: clustering_estimated_dbu at/above :warn_clustering_dbu (WARN) or :crit_clustering_dbu (CRITICAL) - field heuristic; also compare clustered_bytes against removed_bytes - if clustered_bytes is small relative to removed_bytes and DBU is high, clustering may be thrashing rather than compacting.
-- actions: 1) check po_clustering_column_churn for the same table to see if the clustering keys are unstable (free); 2) if the table clusters constantly because of small/streaming writes, batch writes to reduce trigger frequency or lengthen the target file size (config); 3) re-evaluate the CLUSTER BY keys for the table's actual query patterns to reduce re-clustering work (redesign/spend).
-- next: po_clustering_column_churn (if churn is suspected), po_maintenance_cost_by_table (for the full per-table maintenance-cost picture across all PO operation types)
-- caveats: CLUSTERING operations expose exactly the 4 operation_metrics subfields queried here (this is incremental liquid clustering); all are map<string,string> so they are CAST to numeric here. This is a different operation_type from AUTO_CLUSTERING_COLUMN_SELECTION (see po_clustering_column_churn), which has different subfields entirely - do not mix the two. usage_quantity (clustering_estimated_dbu here) is ESTIMATED_DBU, not dollars, and is summed only. The :warn_clustering_dbu/:crit_clustering_dbu bands are judged per (table, workspace) - a table clustered from two workspaces in the window gets two rows, each banded on its own workspace's DBU share, not the table's account-wide total. system.storage.predictive_optimization_operations_history is Public Preview and regional - run it per metastore region.
-- The clustering_estimated_dbu (usage_quantity) cost field lags ~24h, so very recent clustering appears with no or understated DBU and may misband; PO covers only UC managed tables with Predictive Optimization enabled - external tables never appear here.
SELECT workspace_id, catalog_name, schema_name, table_id, table_name, operation_type,
       COUNT(*) AS op_count,
       SUM(CAST(operation_metrics['number_of_removed_files']        AS BIGINT)) AS removed_files,
       SUM(CAST(operation_metrics['number_of_clustered_files']      AS BIGINT)) AS clustered_files,
       SUM(CAST(operation_metrics['amount_of_data_removed_bytes']   AS BIGINT)) AS removed_bytes,
       SUM(CAST(operation_metrics['amount_of_clustered_data_bytes'] AS BIGINT)) AS clustered_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS clustering_estimated_dbu,
       -- status: worst-first band on clustering DBU spend per table (field heuristic; :warn_clustering_dbu / :crit_clustering_dbu).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :crit_clustering_dbu THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :warn_clustering_dbu THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.storage.predictive_optimization_operations_history
WHERE operation_type = 'CLUSTERING'
  AND start_time >= current_date() - INTERVAL :period_days DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name, operation_type
ORDER BY clustering_estimated_dbu DESC
