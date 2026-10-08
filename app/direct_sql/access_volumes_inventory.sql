-- generated from dbt/models/databricks_direct/governance_access/d_access_volumes_inventory.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_volumes_inventory.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH vt AS (
  SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
         COUNT(*) AS tag_count,
         array_join(collect_set(TAG_NAME), ', ') AS tag_names
  FROM `system`.`information_schema`.`volume_tags`
  GROUP BY CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME
)
SELECT
  v.VOLUME_CATALOG AS volume_catalog,
  v.VOLUME_SCHEMA  AS volume_schema,
  v.VOLUME_NAME    AS volume_name,
  v.VOLUME_TYPE    AS volume_type,
  v.VOLUME_OWNER AS volume_owner,
  v.STORAGE_LOCATION AS storage_location,
  COALESCE(vt.tag_count, 0) AS tag_count,
  vt.tag_names              AS tag_names,
  v.CREATED                 AS created,
  CASE
    WHEN COALESCE(vt.tag_count, 0) = 0 AND upper(v.VOLUME_TYPE) = 'EXTERNAL' THEN 'CRITICAL'
    WHEN COALESCE(vt.tag_count, 0) = 0                                       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM `system`.`information_schema`.`volumes` v
LEFT JOIN vt
  ON vt.CATALOG_NAME = v.VOLUME_CATALOG AND vt.SCHEMA_NAME = v.VOLUME_SCHEMA AND vt.VOLUME_NAME = v.VOLUME_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  v.VOLUME_CATALOG, v.VOLUME_SCHEMA, v.VOLUME_NAME
LIMIT 500
) q
