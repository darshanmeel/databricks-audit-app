{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/governance_access/access_pii_outside_tables.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
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
  FROM {{ source('system_information_schema', 'volumes') }} v
  LEFT JOIN (
    SELECT CATALOG_NAME, SCHEMA_NAME, VOLUME_NAME,
           COUNT(*) AS tag_count,
           MAX(CASE WHEN TAG_NAME  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
                      OR TAG_VALUE RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
                    THEN 1 ELSE 0 END) AS is_sensitive,
           array_join(collect_set(TAG_NAME), ', ') AS tag_names
    FROM {{ source('system_information_schema', 'volume_tags') }}
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
  FROM {{ source('system_information_schema', 'schema_tags') }} st
  WHERE st.TAG_NAME  RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
     OR st.TAG_VALUE RLIKE '(?i)(pii|sensitiv|confidential|gdpr|personal|secret|restricted)'
  GROUP BY st.CATALOG_NAME, st.SCHEMA_NAME
)
WHERE is_sensitive = 1 OR untagged = 1   -- findings only (drop tagged-but-non-sensitive volumes)
ORDER BY
  CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
  object_type, catalog_name, schema_name, object_name
LIMIT {{ param('access_pii_outside_tables', 'top_n', 500) }}
) q
