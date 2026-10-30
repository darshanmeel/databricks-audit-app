{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:compute', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/compute/node_types_reference.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT node_type, core_count, memory_mb, gpu_count, account_id
FROM {{ source('system_compute', 'node_types') }}
ORDER BY node_type
) q
