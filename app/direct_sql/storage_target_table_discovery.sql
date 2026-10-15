-- generated from dbt/models/databricks_direct/storage/d_storage_target_table_discovery.sql by tools/build_direct_sql.py; edit the query, never this file.
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
FROM `system`.`information_schema`.`tables`
WHERE table_type <> 'VIEW'
ORDER BY table_catalog, table_schema, table_name
) q
