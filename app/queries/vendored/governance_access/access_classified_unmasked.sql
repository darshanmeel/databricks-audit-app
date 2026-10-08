-- query_id: access_classified_unmasked
-- title: Classified-sensitive columns with no mask applied
-- domain: governance_access   tier: standard
-- reads: system.data_classification.results, system.information_schema.column_masks, system.information_schema.abac_policy_definitions
-- requires: SELECT on system.data_classification and system.information_schema; system.data_classification is Public Preview and requires Unity Catalog; column_masks is Public Preview (DBR 12.2 LTS+); abac_policy_definitions is Public Preview
-- empty_if: schema_not_enabled, preview_unavailable, no_serverless, abac_only, privilege_scoped
-- params: none - fixed join on catalog/schema/table/column, no rolling window or tunable threshold; status is a direct yes/no on whether a mask exists for the classified column.
-- confidence: confirmed
-- confidence_note: Columns are doc-confirmed. The residual uncertainty is join completeness (column_masks is privilege-aware), not whether the columns exist.
-- read_this: One row = a HIGH-confidence classified (PII/sensitive) column and whether a column mask covers it. The column that matters is is_unmasked - true means the auto-classifier is confident the column is sensitive, no masking function is set on it, and no ABAC column-mask policy is attached to its table, schema, catalog or metastore. abac_mask_policies counts the ABAC column-mask policies attached above the column; a column with none set directly but one or more of these reads NOT_ASSESSED, because whether a policy masks it depends on the policy's tag conditions, which are not evaluated here.
-- healthy: status = OK; is_unmasked = false (a mask is applied) - field heuristic; confirm by reviewing MASK_NAME per column.
-- investigate_if: status = CRITICAL when is_unmasked = true - field heuristic; prioritize columns with the widest access (cross-check against access_grants_inventory).
-- actions: 1) confirm the column is genuinely sensitive by reviewing the class_tag/confidence pair, not the raw values (free); 2) attach a column mask function via ALTER TABLE ... ALTER COLUMN ... SET MASK (config); 3) if masking many columns at scale, standardize on a small set of reusable mask functions instead of one-offs (spend/eng time).
-- not_assessed_reasons: abac_policy_in_scope: an ABAC column-mask policy is attached above this column, and whether it masks the column depends on its tag conditions
-- next: access_column_masks_inventory (see every mask already in place), access_pii_propagation_untagged (check whether this same sensitive data propagates further, still unmasked)
-- caveats: column_masks is metastore-scoped, privilege-aware, and excludes BROWSE-only tables - a missing mask row can mean the principal running this query simply cannot see it, not that no mask exists. Treat a low unmasked count as a floor, not a guarantee of a clean masking posture, unless the querying principal has broad visibility. Confirm whether system.information_schema in your account covers the whole metastore or only the system catalog - a per-catalog union may be needed for full coverage. column_masks is Public Preview and requires DBR 12.2 LTS+.
-- column_masks lists only manually-applied (ALTER TABLE SET MASK) masks; ABAC tag-based policy masks never appear there, so ABAC policies are read from abac_policy_definitions (Public Preview; the export identity sees only policies on securables where it has READ METADATA or MANAGE, so an empty result can mean "not visible", not "none").
-- The join used to be written on cm.CATALOG_NAME / cm.SCHEMA_NAME, which follows some published Databricks docs but does not exist on the live table (DEC-53): the real column_masks columns are table_catalog / table_schema / table_name / column_name / mask_name, the same names access_column_masks_inventory.sql already confirmed live - this query now joins on those, matching that sibling. mask_name is selected with an explicit lowercase alias so the output column name is deterministic on every engine: an unaliased column reference can otherwise surface in whatever case it was WRITTEN in the query text on some engines (not the case of the underlying catalog column), which would have silently produced an uppercase MASK_NAME key in production while this repo's own DuckDB-backed tests kept returning lowercase mask_name - the same class of engine-vs-fixture drift DEC-53 already flagged once for this query.
WITH abac_masks AS (
  -- ABAC column-mask policies and where they are attached (metastore, catalog, schema or table)
  SELECT on_securable_type, catalog_name, schema_name, securable_name
  FROM system.information_schema.abac_policy_definitions
  WHERE policy_type = 'COLUMN_MASK'
),
sensitive AS (
  SELECT catalog_name, schema_name, table_name, column_name, class_tag, confidence
  FROM system.data_classification.results
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
  LEFT JOIN system.information_schema.column_masks cm
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
