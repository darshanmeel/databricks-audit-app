-- generated from dbt/models/databricks_direct/governance_access/d_access_column_masks_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_column_masks_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_name, column_name,
       mask_name, using_columns
FROM `system`.`information_schema`.`column_masks`
ORDER BY table_catalog, table_schema, table_name, column_name
) q
