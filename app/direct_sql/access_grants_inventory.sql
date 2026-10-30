-- generated from dbt/models/databricks_direct/governance_access/d_access_grants_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_grants_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT object_scope, PRIVILEGE_TYPE,
       GRANTEE AS GRANTEE,
       grant_count, distinct_objects
FROM (
  SELECT 'TABLE' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT TABLE_CATALOG || '.' || TABLE_SCHEMA || '.' || TABLE_NAME) AS distinct_objects
  FROM `system`.`information_schema`.`table_privileges`
  GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'CATALOG' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT CATALOG_NAME) AS distinct_objects
  FROM `system`.`information_schema`.`catalog_privileges`
  GROUP BY 1, 2, 3
)
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
) q
