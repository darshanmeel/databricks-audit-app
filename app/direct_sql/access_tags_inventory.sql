-- generated from dbt/models/databricks_direct/governance_access/d_access_tags_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_tags_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
SELECT 'COLUMN' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`column_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'TABLE' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`table_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'SCHEMA' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`schema_tags`
GROUP BY 1, 2, 3
UNION ALL
SELECT 'VOLUME' AS object_scope, TAG_NAME, TAG_VALUE,
       COUNT(*) AS tagged_object_count
FROM `system`.`information_schema`.`volume_tags`
GROUP BY 1, 2, 3
ORDER BY object_scope, TAG_NAME, TAG_VALUE
) q
