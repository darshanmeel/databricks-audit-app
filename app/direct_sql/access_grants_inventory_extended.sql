-- generated from dbt/models/databricks_direct/governance_access/d_access_grants_inventory_extended.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_grants_inventory_extended.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT 'SCHEMA' AS object_scope, PRIVILEGE_TYPE,
       GRANTEE AS GRANTEE,
       COUNT(*) AS grant_count
FROM `system`.`information_schema`.`schema_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CONNECTION', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`connection_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CREDENTIAL', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`credential_privileges` GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'EXTERNAL_LOCATION', PRIVILEGE_TYPE,
       GRANTEE,
       COUNT(*)
FROM `system`.`information_schema`.`external_location_privileges` GROUP BY 1,2,GRANTEE
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
) q
