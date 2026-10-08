-- query_id: po_clustering_column_churn
-- title: Clustering-key selection churn by table
-- domain: storage   tier: deep
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_operations_history), regional
-- empty_if: po_not_enabled, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_churn_events (default 2) column-selection-changed events per table over the window that flags WARN; :crit_churn_events (default 5) that flags CRITICAL
-- confidence: confirmed
-- confidence_note: The four operation_metrics subfields (has_column_selection_changed, old_clustering_columns, new_clustering_columns, additional_reason) and their string typing are verified against Databricks system-table docs.
-- read_this: One row = a table + workspace_id (the workspace whose compute ran the selection) + old-to-new clustering-column signature (or "no change") seen at least once in the window. selection_event_count is how many times that exact old-to-new (or unchanged) selection happened; has_column_selection_changed = 'true' rows are where Predictive Optimization actually swapped the clustering keys - repeated true rows on the same table with a rising event count mean the keys keep getting relitigated, which is layout instability, not a one-off tuning pass.
-- healthy: no has_column_selection_changed = 'true' rows, or fewer than :warn_churn_events change events per table over the window (field heuristic - a single key selection/change is normal, especially on a newly clustered table).
-- investigate_if: has_column_selection_changed = 'true' with selection_event_count at/above :warn_churn_events (WARN) or :crit_churn_events (CRITICAL) - field heuristic; repeated churn on the same table means the clustering keys are unstable, which burns DBUs in po_clustering_activity without settling into a stable layout.
-- actions: 1) look at additional_reason and old/new_clustering_columns to see what is driving the reselection (free); 2) if churn tracks a recurring write pattern (e.g. a daily batch that shifts filter columns), pin CLUSTER BY explicitly instead of leaving it to automatic selection (config); 3) if the table's query patterns are genuinely heterogeneous, consider splitting it, or accept the DBU cost as a known trade-off (redesign/spend).
-- next: po_clustering_activity (see the DBU cost this churn is generating), table_props_time_travel_config (check other per-table config while you are looking at this table)
-- caveats: AUTO_CLUSTERING_COLUMN_SELECTION exposes exactly the 4 operation_metrics subfields queried here, and all four are plain STRING values (categorical) - no numeric CAST applies, unlike the CLUSTERING operation_type in po_clustering_activity. old_clustering_columns reads 'None' when the table was previously unpartitioned/unclustered, not an error. has_column_selection_changed = 'true' is what actually indicates churn; 'false' rows just mean Predictive Optimization re-evaluated and kept the same keys. The :warn_churn_events/:crit_churn_events bands are counted per (table, workspace, signature) - a table re-selected from two workspaces in the window gets separate event counts per workspace, not one account-wide total. system.storage.predictive_optimization_operations_history is Public Preview and regional - run per metastore region.
-- AUTO_CLUSTERING_COLUMN_SELECTION rows appear only where Predictive Optimization is enabled (ON by default for accounts >= 2024-11-11, older accounts still rolling out); a table with no rows may mean PO is off, not that its clustering is proven stable.
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['has_column_selection_changed'] AS has_column_selection_changed,
       operation_metrics['old_clustering_columns']        AS old_clustering_columns,
       operation_metrics['new_clustering_columns']        AS new_clustering_columns,
       operation_metrics['additional_reason']             AS additional_reason,
       MAX(end_time) AS last_selection_time,
       COUNT(*)      AS selection_event_count,
       -- status: worst-first band on repeated true churn events per table (field heuristic; :warn_churn_events / :crit_churn_events).
       CASE
         WHEN operation_metrics['has_column_selection_changed'] IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND COUNT(*) >= :crit_churn_events THEN 'CRITICAL'
         WHEN operation_metrics['has_column_selection_changed'] = 'true' AND COUNT(*) >= :warn_churn_events THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.storage.predictive_optimization_operations_history
WHERE operation_type = 'AUTO_CLUSTERING_COLUMN_SELECTION'
  AND start_time >= current_date() - INTERVAL :period_days DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['has_column_selection_changed'], operation_metrics['old_clustering_columns'],
         operation_metrics['new_clustering_columns'], operation_metrics['additional_reason']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, selection_event_count DESC
