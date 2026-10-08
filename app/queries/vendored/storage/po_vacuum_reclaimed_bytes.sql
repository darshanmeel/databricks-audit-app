-- query_id: po_vacuum_reclaimed_bytes
-- title: VACUUM reclaimed bytes and DBU cost by table
-- domain: storage   tier: standard
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_operations_history), regional
-- empty_if: po_not_enabled, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_noop_dbu (default 5) estimated VACUUM DBUs spent on a table that reclaimed zero bytes over the window that flags WARN; :crit_noop_dbu (default 20) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: The two VACUUM operation_metrics subfields (number_of_deleted_files, amount_of_data_deleted_bytes) and their map<string,string> typing are verified against Databricks system-table docs.
-- read_this: One row = a table's successful VACUUM activity over the window in one workspace (workspace_id, the workspace whose compute ran the VACUUM): how many VACUUM operations ran (vacuum_op_count), how many files and bytes they deleted (total_deleted_files, total_deleted_bytes), and the estimated DBUs they cost (vacuum_estimated_dbu). The pairing that matters is total_deleted_bytes against vacuum_estimated_dbu - a table that keeps vacuuming successfully but reclaiming (near) zero bytes is paying maintenance cost for no storage benefit.
-- healthy: total_deleted_bytes > 0 for vacuum activity, or vacuum_estimated_dbu under :warn_noop_dbu when reclaimed bytes are zero (field heuristic - tune :warn_noop_dbu for your account; a well-tuned table that rarely deletes rows can legitimately show low/zero reclaim).
-- investigate_if: total_deleted_bytes = 0 (no reclaim) with vacuum_estimated_dbu at/above :warn_noop_dbu (WARN) or :crit_noop_dbu (CRITICAL) - field heuristic; note this query only covers tables that WERE successfully vacuumed - a table missing entirely from this result may simply never run VACUUM, which is a coverage gap you can only see by pairing this against storage_breakdown_analyze's vacuumable_bytes for the same table.
-- actions: 1) for zero-reclaim tables, check whether Predictive Optimization VACUUM is running on a schedule shorter than your actual delete/update cadence (free); 2) lengthen the VACUUM interval or disable automatic VACUUM for tables that rarely delete data (config); 3) for large tables with genuinely high churn, confirm delta.deletedFileRetentionDuration is not set so short that VACUUM runs (and costs DBU) far more often than needed (config/spend, see table_props_time_travel_config).
-- next: storage_breakdown_analyze (pair reclaimed bytes against vacuumable_bytes still on disk for the same table), table_props_time_travel_config (check the retention config driving VACUUM eligibility)
-- caveats: VACUUM exposes exactly 2 documented operation_metrics subfields - number_of_deleted_files and amount_of_data_deleted_bytes - both map<string,string> so both are CAST to BIGINT here. Databricks Delta VACUUM has no fail-safe: deleted files are gone, not recoverable, so a large total_deleted_bytes is not itself a red flag, it just means VACUUM did its job. This query only returns tables with at least one SUCCESSFUL VACUUM in the window - a table that never runs VACUUM at all will not appear here; use storage_breakdown_analyze's vacuumable_bytes to spot that coverage gap instead. The :warn_noop_dbu/:crit_noop_dbu no-reclaim band is judged per (table, workspace) - a table vacuumed from two workspaces in the window gets two rows, each banded on its own workspace's DBU and reclaimed bytes, not the table's account-wide total. system.storage.predictive_optimization_operations_history is Public Preview and regional.
-- Rows exist only where Predictive Optimization is enabled (default-on for accounts created on/after 2024-11-11, older accounts still rolling out) and only for UC managed tables, so PO-disabled tables and all external tables never appear here regardless of VACUUM activity.
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       COUNT(*) AS vacuum_op_count,
       SUM(CAST(operation_metrics['number_of_deleted_files']      AS BIGINT))  AS total_deleted_files,
       SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT))  AS total_deleted_bytes,
       SUM(CAST(usage_quantity AS DECIMAL(38,6)))                              AS vacuum_estimated_dbu,
       -- status: worst-first band on DBU spent with zero bytes reclaimed (field heuristic; :warn_noop_dbu / :crit_noop_dbu).
       CASE
         WHEN SUM(CAST(usage_quantity AS DECIMAL(38,6))) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :crit_noop_dbu THEN 'CRITICAL'
         WHEN SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS BIGINT)) = 0
              AND SUM(CAST(usage_quantity AS DECIMAL(38,6))) >= :warn_noop_dbu THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.storage.predictive_optimization_operations_history
WHERE operation_type = 'VACUUM' AND operation_status = 'SUCCESSFUL'
  AND start_time >= current_date() - INTERVAL :period_days DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, vacuum_estimated_dbu DESC
