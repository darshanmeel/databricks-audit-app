-- generated from dbt/models/databricks_direct/governance_access/d_access_views_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_views_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT
  TABLE_CATALOG AS view_catalog,
  TABLE_SCHEMA  AS view_schema,
  TABLE_NAME    AS view_name,
  IS_MATERIALIZED    AS is_materialized,
  IS_UPDATABLE       AS is_updatable,
  IS_INSERTABLE_INTO AS is_insertable_into,
  SQL_PATH           AS sql_path,
  LENGTH(VIEW_DEFINITION) AS definition_chars,
  CASE WHEN upper(VIEW_DEFINITION) LIKE '%MASK%' OR upper(VIEW_DEFINITION) LIKE '%FILTER%'
       THEN true ELSE false END AS references_mask_or_filter
FROM `system`.`information_schema`.`views`
ORDER BY view_catalog, view_schema, view_name
LIMIT 100000
) q
