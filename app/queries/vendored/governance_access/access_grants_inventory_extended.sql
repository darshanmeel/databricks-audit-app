-- query_id: access_grants_inventory_extended
-- title: Schema, connection, credential, and external-location grants inventory
-- domain: governance_access   tier: standard
-- reads: system.information_schema.schema_privileges, system.information_schema.connection_privileges, system.information_schema.credential_privileges, system.information_schema.external_location_privileges
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped
-- params: none - current-state grant rollup, no window.
-- confidence: needs_confirmation
-- confidence_note: Only table_privileges and catalog_privileges (used in access_grants_inventory) are transcribed column-by-column; the object-name columns on these four sibling views (e.g. schema_privileges.catalog_name+schema_name, and the connection/credential/external-location name columns) are inferred, not verified. This query only reads GRANTEE and PRIVILEGE_TYPE, which are shared across every privilege view, so it is safe as written - do not add any object-name column from these views until you DESCRIBE it in your workspace.
-- read_this: One row = an object scope (SCHEMA, CONNECTION, CREDENTIAL, or EXTERNAL_LOCATION) x privilege type x grantee, with a grant count. Use it to see who holds broad integration-level access beyond plain table/catalog grants.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: access_grants_inventory (table/catalog grants), access_runas_escalation
-- caveats: These four views exist in information_schema per Databricks documentation, but their exact column names beyond the shared GRANTOR/GRANTEE/PRIVILEGE_TYPE/IS_GRANTABLE/INHERITED_FROM shape are not verified column-by-column here - only GRANTEE and PRIVILEGE_TYPE are used, which are safe. STORAGE_CREDENTIAL_PRIVILEGES is deprecated and excluded on purpose. The same privilege-aware incompleteness caveat as access_grants_inventory applies: this is partial, not a complete grant graph.
-- SAFE as written (uses only GRANTEE/PRIVILEGE_TYPE, shared across all privilege views).
-- NEEDS CONFIRMATION before reading any per-view object-name column: DESCRIBE each sibling view in your own workspace first.
SELECT 'SCHEMA' AS object_scope, PRIVILEGE_TYPE,
       CASE
         WHEN GRANTEE IS NULL OR GRANTEE = '__REDACTED__' THEN GRANTEE
         WHEN GRANTEE RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN GRANTEE
         ELSE concat(substr(sha2(lower(trim(GRANTEE)), 256), 1, 8), ' ', substr(GRANTEE, 1, 2), '***')
       END AS GRANTEE,
       COUNT(*) AS grant_count
FROM system.information_schema.schema_privileges GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CONNECTION', PRIVILEGE_TYPE,
       CASE
         WHEN GRANTEE IS NULL OR GRANTEE = '__REDACTED__' THEN GRANTEE
         WHEN GRANTEE RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN GRANTEE
         ELSE concat(substr(sha2(lower(trim(GRANTEE)), 256), 1, 8), ' ', substr(GRANTEE, 1, 2), '***')
       END,
       COUNT(*)
FROM system.information_schema.connection_privileges GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'CREDENTIAL', PRIVILEGE_TYPE,
       CASE
         WHEN GRANTEE IS NULL OR GRANTEE = '__REDACTED__' THEN GRANTEE
         WHEN GRANTEE RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN GRANTEE
         ELSE concat(substr(sha2(lower(trim(GRANTEE)), 256), 1, 8), ' ', substr(GRANTEE, 1, 2), '***')
       END,
       COUNT(*)
FROM system.information_schema.credential_privileges GROUP BY 1,2,GRANTEE
UNION ALL
SELECT 'EXTERNAL_LOCATION', PRIVILEGE_TYPE,
       CASE
         WHEN GRANTEE IS NULL OR GRANTEE = '__REDACTED__' THEN GRANTEE
         WHEN GRANTEE RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN GRANTEE
         ELSE concat(substr(sha2(lower(trim(GRANTEE)), 256), 1, 8), ' ', substr(GRANTEE, 1, 2), '***')
       END,
       COUNT(*)
FROM system.information_schema.external_location_privileges GROUP BY 1,2,GRANTEE
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
