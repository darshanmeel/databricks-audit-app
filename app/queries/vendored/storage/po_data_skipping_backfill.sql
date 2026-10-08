-- query_id: po_data_skipping_backfill
-- title: Data-skipping stats backfill by table
-- domain: storage   tier: deep
-- reads: system.storage.predictive_optimization_operations_history
-- requires: SELECT on system.storage; Public Preview (system.storage.predictive_optimization_operations_history), regional
-- empty_if: po_not_enabled, schema_not_enabled, preview_unavailable
-- params: :period_days (default 30) rolling window in days; :warn_scan_gb (default 100) GB scanned by a backfill event that added no new data-skipping columns, over the window, that flags WARN; :crit_scan_gb (default 500) GB that flags CRITICAL
-- confidence: confirmed
-- confidence_note: DATA_SKIPPING_COLUMN_SELECTION's operation_metrics subfields queried here (added/removed/new_data_skipping_columns, amount_of_scanned_bytes, number_of_scanned_files) are verified against Databricks system-table docs; a 6th real subfield, old_data_skipping_columns, exists but is intentionally not selected here.
-- read_this: One row = a table + workspace_id (the workspace whose compute ran the backfill) + distinct added/removed/new data-skipping-column set seen in the window. scanned_bytes is how many bytes Predictive Optimization had to scan to decide on (or confirm) data-skipping columns for that table in that workspace; added_data_skipping_columns is what it actually added. A row with a large scanned_bytes but an empty added_data_skipping_columns means the backfill ran and paid its scan cost but did not end up adding any new pruning columns for that table.
-- healthy: added_data_skipping_columns is populated whenever a backfill event scans data, or scanned_bytes stays under :warn_scan_gb GB for events that added none (field heuristic - tune :warn_scan_gb for your account).
-- investigate_if: no added_data_skipping_columns AND scanned_bytes at/above :warn_scan_gb GB (WARN) or :crit_scan_gb GB (CRITICAL) - field heuristic; this only tells you stats were (re)computed, not whether pruning actually improved - cross-check query_pruning_effectiveness for the payoff.
-- actions: 1) check query_pruning_effectiveness for the table to see if existing data-skipping columns are actually being used for pruning (free); 2) if the table has many high-cardinality or rarely-filtered columns, explicitly set delta.dataSkippingStatsColumns to a smaller, deliberate list instead of leaving it to automatic selection (config); 3) if this backfill activity recurs expensively with no coverage gain on a large table, consider a clustering/Z-order redesign around the columns you actually filter on (redesign/spend).
-- next: query_pruning_effectiveness (see whether data-skipping columns are actually cutting scanned bytes at query time), po_clustering_activity (compaction/clustering activity on the same table)
-- caveats: DATA_SKIPPING_COLUMN_SELECTION has exactly 6 documented operation_metrics subfields; a 6th, old_data_skipping_columns, is real but not selected in this query (an intentional omission, not a correctness issue - add it yourself if you want the prior column set). Byte/file fields are CAST to numeric; the column-list fields (added/removed/new_data_skipping_columns) stay STRING. Gain is judged from added_data_skipping_columns, not new_data_skipping_columns - real DATA_SKIPPING_COLUMN_SELECTION events do not populate the latter at all, so a fix keyed on it would flag every event as "no gain" regardless of what was actually added; new_data_skipping_columns is still surfaced for reference in case your account's events do populate it. A row reads NOT_ASSESSED, not OK, when added/removed/new are ALL NULL - that shape means the event's metrics payload carries no gain information at all, not that gain was measured and found to be zero. This query does NOT tell you the actual prune ratio or whether the resulting data-skipping columns cut scanned bytes at query time - that lives in system.query.history / query_pruning_effectiveness. Treat a row here only as "stats were (re)computed", never as a measure of pruning benefit. The :warn_scan_gb/:crit_scan_gb bands are judged per (table, workspace, signature) - a table backfilled from two workspaces in the window gets separate scan totals per workspace, not one account-wide total. system.storage.predictive_optimization_operations_history is Public Preview and regional - run per metastore region.
-- Empty is not zero: rows only exist where Predictive Optimization is enabled (ON by default only for accounts created on/after 2024-11-11; older accounts still rolling out) and only for UC managed tables, so external tables and PO-disabled catalogs never appear here.
SELECT workspace_id, catalog_name, schema_name, table_id, table_name,
       operation_metrics['added_data_skipping_columns']   AS added_data_skipping_columns,
       operation_metrics['removed_data_skipping_columns'] AS removed_data_skipping_columns,
       operation_metrics['new_data_skipping_columns']     AS new_data_skipping_columns,
       SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) AS scanned_bytes,
       SUM(CAST(operation_metrics['number_of_scanned_files'] AS BIGINT)) AS scanned_files,
       MAX(end_time) AS last_event_time,
       -- status: worst-first band on scan cost with zero ADDED-column gain (field heuristic; :warn_scan_gb / :crit_scan_gb).
       -- new_data_skipping_columns is not a reliable gain signal (real events don't populate it) - see caveats.
       CASE
         WHEN SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) IS NULL THEN 'NOT_ASSESSED'
         WHEN operation_metrics['added_data_skipping_columns'] IS NULL
              AND operation_metrics['removed_data_skipping_columns'] IS NULL
              AND operation_metrics['new_data_skipping_columns'] IS NULL THEN 'NOT_ASSESSED'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= :crit_scan_gb * 1e9 THEN 'CRITICAL'
         WHEN (operation_metrics['added_data_skipping_columns'] IS NULL OR operation_metrics['added_data_skipping_columns'] = '')
              AND SUM(CAST(operation_metrics['amount_of_scanned_bytes'] AS BIGINT)) >= :warn_scan_gb * 1e9 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.storage.predictive_optimization_operations_history
WHERE operation_type = 'DATA_SKIPPING_COLUMN_SELECTION'
  AND start_time >= current_date() - INTERVAL :period_days DAYS AND start_time < current_date()
GROUP BY workspace_id, catalog_name, schema_name, table_id, table_name,
         operation_metrics['added_data_skipping_columns'], operation_metrics['removed_data_skipping_columns'],
         operation_metrics['new_data_skipping_columns']
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, scanned_bytes DESC
