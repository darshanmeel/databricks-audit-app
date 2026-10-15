-- query_id: node_types_reference
-- title: Node type reference (vCPU / memory / GPU)
-- domain: compute   tier: lite
-- reads: system.compute.node_types
-- requires: SELECT on system.compute; GA
-- params: none (static reference dimension, no time window)
-- confidence: confirmed
-- confidence_note: Columns verified against system.compute.node_types in a live workspace; this is a static reference dimension, not usage data.
-- read_this: One row = one node type's fixed specs (vCPU, memory, GPU count). Join node_type to classic_clusters_config_current.driver_node_type/worker_node_type or to node_timeline_utilization.node_type to turn a node type name into actual capacity.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: classic_clusters_config_current (to see which clusters use a given node_type), node_timeline_utilization (for per-node utilization by node_type)
-- caveats: This is a reference dimension - one row per node type, with no aggregation and no time window. Join on node_type to classic_clusters_config_current.driver_node_type/worker_node_type and to node_timeline_utilization.node_type. core_count is a double, memory_mb is a long, gpu_count is a long. Retention is indefinite (this table does not roll off like event or usage tables).
SELECT node_type, core_count, memory_mb, gpu_count, account_id
FROM system.compute.node_types
ORDER BY node_type
