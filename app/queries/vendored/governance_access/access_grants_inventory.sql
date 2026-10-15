-- query_id: access_grants_inventory
-- title: Table and catalog grants inventory
-- domain: governance_access   tier: standard
-- reads: system.information_schema.table_privileges, system.information_schema.catalog_privileges
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped
-- params: none - current-state grant rollup, no window.
-- confidence: confirmed
-- confidence_note: table_privileges and catalog_privileges are transcribed verbatim - the only two privilege views confirmed column-by-column for this query.
-- read_this: One row = an object scope (TABLE or CATALOG) x privilege type x grantee, rolled up. The columns that matter are grant_count (how many individual grants) and distinct_objects (how many different tables/catalogs that grantee touches).
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: access_grants_inventory_extended (schema/connection/credential/external-location grants), access_runas_escalation (cross-check a broadly-granted identity against run-as activity)
-- caveats: system.information_schema is privilege-aware - a principal with MANAGE sees only its own grants and only the objects it can see, so a single service principal generally cannot enumerate every grant in the metastore. Even run as a high-privilege audit principal, label this "partial - privilege-aware; incomplete vs SHOW GRANTS", never a complete grant graph. IS_GRANTABLE is always 'NO' (reserved by Databricks) and is not collected here.
-- table_privileges carries an inherited_from column (set when the grant traces to a higher-scope grant, e.g. a catalog-level grant that also covers this table) which this query does NOT filter or split out - grant_count and distinct_objects on the TABLE scope pool EXPLICIT and INHERITED-from-a-higher-scope grants together, undifferentiated (contrary to an earlier version of this caveat, which wrongly claimed inherited grants "never appear as rows" here - they do, just not labelled as such). catalog_privileges carries no inherited_from column in this source at all, so a catalog-level grant inherited from, say, a metastore default is invisible on the CATALOG scope, not zero. Privileges derived from object OWNERSHIP (never an explicit or inherited GRANT row on either scope) still never appear as rows in either branch, so a grantee's effective access can still exceed grant_count even after accounting for the above.
SELECT object_scope, PRIVILEGE_TYPE,
       CASE
         WHEN GRANTEE IS NULL OR GRANTEE = '__REDACTED__' THEN GRANTEE
         WHEN GRANTEE RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN GRANTEE
         ELSE concat(substr(sha2(lower(trim(GRANTEE)), 256), 1, 8), ' ', substr(GRANTEE, 1, 2), '***')
       END AS GRANTEE,
       grant_count, distinct_objects
FROM (
  SELECT 'TABLE' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT TABLE_CATALOG || '.' || TABLE_SCHEMA || '.' || TABLE_NAME) AS distinct_objects
  FROM system.information_schema.table_privileges
  GROUP BY 1, 2, 3
  UNION ALL
  SELECT 'CATALOG' AS object_scope, PRIVILEGE_TYPE, GRANTEE,
         COUNT(*) AS grant_count,
         COUNT(DISTINCT CATALOG_NAME) AS distinct_objects
  FROM system.information_schema.catalog_privileges
  GROUP BY 1, 2, 3
)
ORDER BY object_scope, PRIVILEGE_TYPE, GRANTEE
