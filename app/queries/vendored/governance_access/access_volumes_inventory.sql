-- query_id: access_volumes_inventory
-- title: UC volumes inventory and tag coverage (files outside tables)
-- domain: governance_access   tier: standard
-- reads: system.information_schema.volumes, system.information_schema.volume_tags
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped, no_activity
-- params: :top_n (default 500) row cap - current-state inventory, no window.
-- confidence: confirmed
-- confidence_note: volumes and volume_tags columns are transcribed verbatim from the information_schema reference.
-- read_this: One row = one Unity Catalog volume (files that live OUTSIDE tables), with its type, storage location, and whether it carries any governance tag. The column that matters is tag_count = 0 - an unclassified file store, a common PII blind spot, since data classification and column masks apply to TABLES, not volume files.
-- healthy: tag_count above 0 - the volume carries at least one governance tag (field heuristic).
-- investigate_if: tag_count = 0 on an EXTERNAL volume (CRITICAL - unclassified files in external storage) or on a managed volume (WARN - unclassified file store) - field heuristic; tune to your tagging policy.
-- actions: 1) tag volumes that hold sensitive files with your classification tags so they appear in governance rollups (free/config); 2) for external volumes confirm the storage_location is an intended, access-controlled location (config); 3) move PII that belongs in governed tables out of raw volume files (spend/process).
-- next: access_tags_inventory (the account's tag vocabulary these should use), access_data_classification_inventory (table-level classification these volume files are invisible to)
-- caveats: Volumes hold FILES, which sit outside the table-level classification and masking the other governance queries cover - so an untagged volume is a genuine blind spot, not a false positive. system.information_schema is privilege-aware, so volumes the principal cannot see are absent. tag_count is DISTINCT tags from volume_tags joined on (catalog, schema, volume). storage_location is the raw external/managed path and is SENSITIVE - treat the result as confidential. volume_owner is partial-masked. Current-state inventory (no window).
WITH vt AS (
  SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
         COUNT(*) AS tag_count,
         array_join(collect_set(TAG_NAME), ', ') AS tag_names
  FROM system.information_schema.volume_tags
  GROUP BY CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME
)
SELECT
  v.VOLUME_CATALOG AS volume_catalog,
  v.VOLUME_SCHEMA  AS volume_schema,
  v.VOLUME_NAME    AS volume_name,
  v.VOLUME_TYPE    AS volume_type,
  CASE WHEN v.VOLUME_OWNER IS NULL OR v.VOLUME_OWNER = '__REDACTED__' THEN v.VOLUME_OWNER
       WHEN v.VOLUME_OWNER RLIKE '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN v.VOLUME_OWNER
       ELSE concat(substr(sha2(lower(trim(v.VOLUME_OWNER)), 256), 1, 8), ' ', substr(v.VOLUME_OWNER, 1, 2), '***') END AS volume_owner,
  v.STORAGE_LOCATION AS storage_location,
  COALESCE(vt.tag_count, 0) AS tag_count,
  vt.tag_names              AS tag_names,
  v.CREATED                 AS created,
  CASE
    WHEN COALESCE(vt.tag_count, 0) = 0 AND upper(v.VOLUME_TYPE) = 'EXTERNAL' THEN 'CRITICAL'
    WHEN COALESCE(vt.tag_count, 0) = 0                                       THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM system.information_schema.volumes v
LEFT JOIN vt
  ON vt.CATALOG_NAME = v.VOLUME_CATALOG AND vt.SCHEMA_NAME = v.VOLUME_SCHEMA AND vt.VOLUME_NAME = v.VOLUME_NAME
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  v.VOLUME_CATALOG, v.VOLUME_SCHEMA, v.VOLUME_NAME
LIMIT :top_n
