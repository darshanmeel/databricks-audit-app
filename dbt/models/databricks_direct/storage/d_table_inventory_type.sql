{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:storage', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/storage/table_inventory_type.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_type, data_source_format,
       COUNT(*) AS table_count
FROM {{ source('system_information_schema', 'tables') }}
GROUP BY table_catalog, table_schema, table_type, data_source_format
ORDER BY table_catalog, table_schema, table_type
) q
