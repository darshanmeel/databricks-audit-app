-- generated from dbt/models/databricks_direct/compute/d_classic_clusters_config_current.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/classic_clusters_config_current.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT cluster_id,
       cluster_name AS cluster_name,
       owned_by AS owned_by,
       driver_node_type, worker_node_type, worker_count,
       min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, enable_elastic_disk,
       cluster_source, dbr_version, data_security_mode, policy_id, driver_instance_pool_id,
       worker_instance_pool_id, tags, init_scripts, aws_attributes, azure_attributes, gcp_attributes,
       create_time, delete_time, change_time, workspace_id, account_id
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`clusters`
)
WHERE rn = 1 AND delete_time IS NULL
ORDER BY cluster_id
) q
