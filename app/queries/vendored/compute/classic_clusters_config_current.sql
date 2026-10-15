-- query_id: classic_clusters_config_current
-- title: Classic cluster configuration (current)
-- domain: compute   tier: lite
-- reads: system.compute.clusters
-- requires: SELECT on system.compute; GA
-- empty_if: compute_scope_gap
-- params: none (config snapshot, no time window)
-- confidence: confirmed
-- confidence_note: Columns verified against system.compute.clusters in a live workspace; cloud-specific attribute structs (aws_attributes/azure_attributes/gcp_attributes) are documented by example only.
-- read_this: One row = the latest known configuration for one classic cluster (all-purpose, job, Lakeflow SDP, or pipeline-maintenance) that has not been deleted. The columns that matter are data_security_mode (access-mode posture) and policy_id (NULL means the cluster is not governed by a compute policy).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: node_types_reference (to size vCPU/memory/GPU for driver_node_type/worker_node_type), compute_idle_node_ratio (to see idle time on these classic clusters)
-- caveats: Classic compute ONLY (all-purpose, jobs, Lakeflow SDP, pipeline-maintenance) - no serverless, no SQL warehouses (see sql_warehouse_config_current for those). There is no runtime_engine/photon column (confirmed absent on all clouds, so Photon-on-classic cannot be read from this table). data_security_mode enum is USER_ISOLATION / SINGLE_USER / LEGACY_PASSTHROUGH / LEGACY_SINGLE_USER / LEGACY_TABLE_ACL / NONE / null. worker_count is NULL for autoscaling clusters; min_autoscale_workers/max_autoscale_workers are NULL for fixed-size clusters. aws_attributes/azure_attributes/gcp_attributes are STRUCTs selected whole - only the cloud you are actually running on has its struct populated, and pulling one specific subfield out of these structs is needs_confirmation (verify the field exists for your cloud/region before relying on it). Regional - run per metastore region.
SELECT cluster_id,
       cluster_name AS cluster_name,
       CASE
         WHEN owned_by IS NULL OR owned_by = '__REDACTED__' THEN owned_by
         WHEN owned_by RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN owned_by
         ELSE concat(substr(sha2(lower(trim(owned_by)), 256), 1, 8), ' ', substr(owned_by, 1, 2), '***')
       END AS owned_by,
       driver_node_type, worker_node_type, worker_count,
       min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, enable_elastic_disk,
       cluster_source, dbr_version, data_security_mode, policy_id, driver_instance_pool_id,
       worker_instance_pool_id, tags, init_scripts, aws_attributes, azure_attributes, gcp_attributes,
       create_time, delete_time, change_time, workspace_id, account_id
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
  FROM system.compute.clusters
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY cluster_id
