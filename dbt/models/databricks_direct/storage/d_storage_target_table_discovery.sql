{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/storage/storage_target_table_discovery.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
    table_catalog || '.' || table_schema || '.' || table_name AS target_table,
    table_catalog,
    table_schema,
    table_name,
    table_type,
    data_source_format
FROM {{ source('system_information_schema', 'tables') }}
WHERE table_type <> 'VIEW'
ORDER BY table_catalog, table_schema, table_name
) q
