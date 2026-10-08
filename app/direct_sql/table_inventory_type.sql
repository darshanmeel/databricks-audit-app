-- generated from dbt/models/databricks_direct/storage/d_table_inventory_type.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/storage/table_inventory_type.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_type, data_source_format,
       COUNT(*) AS table_count
FROM `system`.`information_schema`.`tables`
GROUP BY table_catalog, table_schema, table_type, data_source_format
ORDER BY table_catalog, table_schema, table_type
) q
