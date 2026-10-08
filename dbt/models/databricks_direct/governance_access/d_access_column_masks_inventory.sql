{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_column_masks_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_name, column_name,
       mask_name, using_columns
FROM {{ source('system_information_schema', 'column_masks') }}
ORDER BY table_catalog, table_schema, table_name, column_name
) q
