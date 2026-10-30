-- generated from dbt/models/databricks_direct/governance_access/d_access_row_filters_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_row_filters_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT table_catalog, table_schema, table_name,
       filter_name, target_columns
FROM `system`.`information_schema`.`row_filters`
ORDER BY table_catalog, table_schema, table_name
) q
