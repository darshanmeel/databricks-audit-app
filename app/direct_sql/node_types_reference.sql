-- generated from dbt/models/databricks_direct/compute/d_node_types_reference.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/node_types_reference.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT node_type, core_count, memory_mb, gpu_count, account_id
FROM `system`.`compute`.`node_types`
ORDER BY node_type
) q
