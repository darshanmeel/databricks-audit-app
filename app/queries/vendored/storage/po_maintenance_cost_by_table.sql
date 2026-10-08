-- query_id: po_maintenance_cost_by_table
-- title: Predictive Optimization maintenance cost and status by table
-- domain: storage   tier: standard
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_operations_history), regional, 180-day retention
-- empty_if: schema_not_enabled, po_not_enabled, preview_unavailable, ingestion_lag
-- params: :period_days (default 30) rolling window in days; :warn_maint_dbu (default 20) estimated maintenance DBUs per table+operation+status group over the window that flags WARN; :crit_maint_dbu (default 100) that flags CRITICAL (a FAILED operation_status always flags CRITICAL regardless of these)
-- confidence: confirmed
-- confidence_note: usage_quantity as ESTIMATED_DBU, the operation_status enum values (including the 'FAILED: INTERNAL_ERROR' string with its embedded colon), and the up-to-24h billing-population lag are verified against Databricks docs.
-- read_this: One row = a table x operation_type (CLUSTERING/VACUUM/etc) x operation_status combo over the window. estimated_dbu is the DBUs Predictive Optimization spent maintaining that table for that operation type - the columns that matter are operation_status (SUCCESSFUL vs a 'FAILED: INTERNAL_ERROR' row, note the embedded colon) and estimated_dbu; a table with FAILED rows means maintenance is not actually completing even though you may be paying for the attempts.
-- healthy: no FAILED rows for the table, and estimated_dbu under :warn_maint_dbu per operation+status group over the window (field heuristic - tune :warn_maint_dbu for your account).
-- investigate_if: any operation_status LIKE 'FAILED%' row (CRITICAL - maintenance is not completing on that table), or estimated_dbu at/above :warn_maint_dbu (WARN) / :crit_maint_dbu (CRITICAL) - field heuristic.
-- actions: 1) for FAILED rows, check the table for the underlying error (schema drift, concurrent writers, permissions) before anything else (free); 2) for high-DBU SUCCESSFUL tables, check po_clustering_activity / po_vacuum_reclaimed_bytes for that table_id to see which operation type is driving the cost (free/config); 3) if a large table's maintenance cost is structural, consider a partitioning/clustering redesign, or exclude it from automatic PO and schedule maintenance in an off-peak window instead (spend/redesign).
-- next: po_clustering_activity (drill into CLUSTERING rows for a specific table), po_vacuum_reclaimed_bytes (drill into VACUUM rows for a specific table)
-- caveats: usage_quantity is ESTIMATED_DBU, not dollars - sum only, never average or otherwise combine across rows. operation_status is an enum of SUCCESSFUL or the literal string 'FAILED: INTERNAL_ERROR' (note the embedded colon - do not split on the first colon when parsing). Grouping by operation_status is what lets you compute a per-table success rate. usage_quantity can lag the operation row by up to 24 hours while billing populates - the start_time < current_date() guard drops today's rows but NOT yesterday's still-populating values, so treat the most recent day of this window as provisional. system.storage.predictive_optimization_operations_history is Public Preview, regional, and retains 180 days - do not set :period_days beyond that and expect rows.
-- No rows does not mean zero maintenance cost: Predictive Optimization must be enabled (ON by default only for accounts created on/after 2024-11-11; older accounts are still rolling out) and this table covers only UC managed tables, so external tables and PO-disabled tables never appear.
SELECT account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
       operation_type, operation_status, usage_unit,
       COUNT(*)                                  AS operation_count,
       SUM(CAST(usage_quantity AS DECIMAL(38,6))) AS estimated_dbu,
       MIN(start_time) AS first_op_time, MAX(end_time) AS last_op_time,
       -- status: worst-first band; any FAILED row is CRITICAL, else banded on estimated_dbu (field heuristic; :warn_maint_dbu / :crit_maint_dbu).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_status LIKE 'FAILED%' THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :crit_maint_dbu THEN 'CRITICAL'
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :warn_maint_dbu THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.storage.predictive_optimization_operations_history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
GROUP BY account_id, workspace_id, metastore_name, catalog_name, schema_name, table_id, table_name,
         operation_type, operation_status, usage_unit
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, estimated_dbu DESC
