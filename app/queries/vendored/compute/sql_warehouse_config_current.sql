-- query_id: sql_warehouse_config_current
-- title: SQL warehouse configuration (current)
-- domain: compute   tier: lite
-- reads: system.compute.warehouses
-- requires: SELECT on system.compute; GA
-- params: none (config snapshot, no time window)
-- confidence: confirmed
-- confidence_note: Columns verified against system.compute.warehouses in a live workspace, cross-checked against the full column list (see caveats for a vendor-doc miscount).
-- read_this: One row = the latest known configuration for one SQL warehouse that has not been deleted. The columns that matter are warehouse_size, min_clusters/max_clusters (autoscaling bounds), and auto_stop_minutes (how long the warehouse waits before suspending).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: compute_warehouse_idle_gaps (to see this warehouse's actual RUNNING idle tail against its auto_stop_minutes), compute_warehouse_autoscale_churn (to see if min_clusters/max_clusters is causing thrash), sql_warehouse_events_activity (for the raw event history)
-- caveats: SCD snapshot: this returns the latest row per warehouse_id, so a deleted warehouse (delete_time NOT NULL) is excluded. warehouse_size enum includes 5X_LARGE (Beta, PRO/SERVERLESS channel only). tags is a map. Regional - run per metastore region. One source note: the original vendor documentation for this table says it "has 12 columns" but its own reference table for the same table lists 13 - that is a documentation-internal miscount in the vendor docs, not an error in this query, which selects all 13.
SELECT warehouse_id, warehouse_name AS warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
       warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time
FROM (
  SELECT warehouse_id, warehouse_name, workspace_id, account_id, warehouse_type, warehouse_channel,
         warehouse_size, min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM system.compute.warehouses
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY warehouse_id
