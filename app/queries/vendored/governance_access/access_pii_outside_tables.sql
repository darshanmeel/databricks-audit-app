-- query_id: access_pii_outside_tables
-- title: PII outside governed tables - sensitive-tagged and untagged volumes and schemas
-- domain: governance_access   tier: standard
-- reads: system.information_schema.volumes, system.information_schema.volume_tags, system.information_schema.schema_tags
-- requires: SELECT on system.information_schema; Unity Catalog required
-- empty_if: privilege_scoped, no_activity
-- params: :top_n (default 500) row cap - current-state, no window.
-- confidence: needs_confirmation
-- confidence_note: volumes, volume_tags, and schema_tags columns are transcribed verbatim; the sensitivity match is a HARD-CODED regex heuristic over tag names/values, not an authoritative classifier.
-- read_this: One row = a VOLUME or SCHEMA that either carries a sensitivity/PII tag (known sensitive data living OUTSIDE the table surface, where column masks / row filters / auto-classification do NOT apply) or is an UNTAGGED volume (an unclassified file store). The status band says which. This is the file/schema counterpart to access_classified_unmasked, which only sees table columns.
-- healthy: a tagged, non-sensitivity-tagged volume (field heuristic) - the file store is inventoried and not flagged sensitive.
-- investigate_if: a sensitivity-tagged or untagged EXTERNAL volume (CRITICAL - sensitive/unclassified files in external storage that masks cannot protect); a sensitivity-tagged managed volume or schema, or an untagged managed volume (WARN) - field heuristic; tune the sensitivity regex in the SQL to your tag vocabulary.
-- actions: 1) for sensitive volumes, lock down access via grants + storage credentials (files cannot be masked) and confirm the data belongs in files not governed tables (free/config); 2) tag or classify untagged volumes so they stop being blind spots (config); 3) migrate PII that should be maskable into governed UC tables (spend/process).
-- next: access_volumes_inventory (the full volume inventory this is derived from), access_classified_unmasked (the table-column PII this does NOT cover), access_tags_inventory (the account's tag vocabulary)
-- caveats: Volumes hold FILES and schemas are NAMESPACES - neither is covered by column masks, row filters, or the table-level auto-classifier, so sensitive data here is a genuine governance blind spot, not a false positive. The sensitivity match is a HARD-CODED regex over tag name/value ('(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)') - tune it in the SQL to your tag vocabulary; it will miss org-specific tag names and can false-positive on unrelated ones. system.information_schema is privilege-aware, so volumes/schemas the principal cannot see are absent. storage_location is deliberately not emitted here (see access_volumes_inventory) - this is the finding view. Current-state (no window).
SELECT
  object_type, catalog_name, schema_name, object_name, object_detail, finding, tags,
  CASE
    WHEN is_sensitive = 1 AND is_external = 1 THEN 'CRITICAL'
    WHEN untagged     = 1 AND is_external = 1 THEN 'CRITICAL'
    WHEN is_sensitive = 1                     THEN 'WARN'
    WHEN untagged     = 1                     THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM (
  -- VOLUMES: sensitive-tagged (known PII files) or untagged (blind spot)
  SELECT
    'VOLUME'          AS object_type,
    v.VOLUME_CATALOG  AS catalog_name,
    v.VOLUME_SCHEMA   AS schema_name,
    v.VOLUME_NAME     AS object_name,
    v.VOLUME_TYPE     AS object_detail,
    CASE WHEN upper(v.VOLUME_TYPE) = 'EXTERNAL' THEN 1 ELSE 0 END AS is_external,
    COALESCE(vt.is_sensitive, 0)                                 AS is_sensitive,
    CASE WHEN COALESCE(vt.tag_count, 0) = 0 THEN 1 ELSE 0 END    AS untagged,
    vt.tag_names                                                AS tags,
    CASE WHEN COALESCE(vt.is_sensitive, 0) = 1 THEN 'sensitive-tagged (PII files outside governed tables)'
         WHEN COALESCE(vt.tag_count, 0) = 0    THEN 'untagged (unclassified file store)'
         ELSE 'tagged (non-sensitive)' END                     AS finding
  FROM system.information_schema.volumes v
  LEFT JOIN (
    SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
           COUNT(*) AS tag_count,
           MAX(CASE WHEN TAG_NAME  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
                      OR TAG_VALUE RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
                    THEN 1 ELSE 0 END) AS is_sensitive,
           array_join(collect_set(TAG_NAME), ', ') AS tag_names
    FROM system.information_schema.volume_tags
    GROUP BY CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME
  ) vt
    ON vt.CATALOG_NAME = v.VOLUME_CATALOG AND vt.SCHEMA_NAME = v.VOLUME_SCHEMA AND vt.VOLUME_NAME = v.VOLUME_NAME
  UNION ALL
  -- SCHEMAS: only those carrying a sensitivity tag (a namespace flagged sensitive)
  SELECT
    'SCHEMA'          AS object_type,
    st.CATALOG_NAME   AS catalog_name,
    st.SCHEMA_NAME    AS schema_name,
    st.SCHEMA_NAME    AS object_name,
    'schema tag'      AS object_detail,
    0                 AS is_external,
    1                 AS is_sensitive,
    0                 AS untagged,
    array_join(collect_set(concat(st.TAG_NAME, '=', st.TAG_VALUE)), ', ') AS tags,
    'sensitive-tagged schema (namespace flagged sensitive)'              AS finding
  FROM system.information_schema.schema_tags st
  WHERE st.TAG_NAME  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
     OR st.TAG_VALUE RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
  GROUP BY st.CATALOG_NAME, st.SCHEMA_NAME
)
WHERE is_sensitive = 1 OR untagged = 1   -- findings only (drop tagged-but-non-sensitive volumes)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  object_type, catalog_name, schema_name, object_name
LIMIT :top_n
