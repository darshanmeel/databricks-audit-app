-- generated from dbt/models/databricks_direct/governance_access/d_access_classified_unmasked.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/governance_access/access_classified_unmasked.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH abac_masks AS (
  -- ABAC column-mask policies and where they are attached (metastore, catalog, schema or table)
  SELECT on_securable_type, catalog_name, schema_name, securable_name
  FROM `system`.`information_schema`.`abac_policy_definitions`
  WHERE policy_type = 'COLUMN_MASK'
),
sensitive AS (
  SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence
  FROM `system`.`data_classification`.`results`
  WHERE confidence = 'HIGH'
    AND class_tag IS NOT NULL
),
abac_per_table AS (
  -- a policy covers every table below where it is attached
  SELECT t.catalog_name, t.schema_name, t.table_name, count(*) AS abac_mask_policies
  FROM (SELECT DISTINCT catalog_name, schema_name, table_name FROM sensitive) t
  JOIN abac_masks am
    ON am.on_securable_type = 'METASTORE'
    OR (am.on_securable_type = 'CATALOG' AND am.catalog_name = t.catalog_name)
    OR (am.on_securable_type = 'SCHEMA' AND am.catalog_name = t.catalog_name AND am.schema_name = t.schema_name)
    OR (am.on_securable_type = 'TABLE' AND am.catalog_name = t.catalog_name AND am.schema_name = t.schema_name
        AND am.securable_name = t.table_name)
  GROUP BY t.catalog_name, t.schema_name, t.table_name
),
classified AS (
  SELECT dc.catalog_name, dc.schema_name, dc.table_name, dc.column_name, dc.class_tag, dc.confidence,
         cm.mask_name AS mask_name,
         COALESCE(ap.abac_mask_policies, 0) AS abac_mask_policies
  FROM sensitive dc
  LEFT JOIN `system`.`information_schema`.`column_masks` cm
    ON  cm.table_catalog = dc.catalog_name
    AND cm.table_schema  = dc.schema_name
    AND cm.table_name    = dc.table_name
    AND cm.column_name   = dc.column_name
  LEFT JOIN abac_per_table ap
    ON  ap.catalog_name = dc.catalog_name
    AND ap.schema_name  = dc.schema_name
    AND ap.table_name   = dc.table_name
)
SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence,
       mask_name,
       abac_mask_policies,
       CASE WHEN mask_name IS NULL AND abac_mask_policies = 0 THEN true ELSE false END AS is_unmasked,
       CASE WHEN mask_name IS NULL AND abac_mask_policies > 0 THEN 'abac_policy_in_scope' END AS not_assessed_reason,
       -- status: a mask set on the column is OK; an ABAC policy above it can't be judged per column
       CASE WHEN mask_name IS NOT NULL THEN 'OK'
            WHEN abac_mask_policies > 0 THEN 'NOT_ASSESSED'
            ELSE 'CRITICAL' END AS status
FROM classified
ORDER BY is_unmasked DESC, catalog_name, schema_name, table_name, column_name
) q
