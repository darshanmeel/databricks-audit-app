-- query_id: access_classification_coverage
-- title: Data classification coverage by catalog - how much of the data has been scanned
-- domain: governance_access   tier: standard
-- reads: system.information_schema.tables, system.data_classification.results
-- requires: SELECT on system.information_schema and system.data_classification; system.information_schema needs Unity Catalog; system.data_classification.results is Public Preview and needs BOTH the data-classification feature AND the system.data_classification schema enabled
-- empty_if: schema_not_enabled, preview_unavailable, no_serverless
-- params: :warn_coverage_pct (default 80) percent of a catalog's tables with any system.data_classification.results row, below which the catalog reads WARN
-- confidence: needs_confirmation
-- confidence_note: information_schema.tables and data_classification.results columns are confirmed against Databricks documentation (also used by storage_target_table_discovery and access_data_classification_inventory in this library); "any row = scanned" is this query's own definition of coverage, not a documented status column, since the system table carries no per-table "scanned, nothing found" marker - only detections.
-- read_this: One row = a catalog. tables_total is the catalog's own tables from information_schema (views excluded); tables_classified is how many of those tables have at least one row in system.data_classification.results, for any class_tag. A table that was scanned and found clean looks identical to one never scanned, since the source table only records detections - so covered_pct is a floor on scan activity, not proof of what was or was not scanned. covered_pct is NULL and status is NOT_ASSESSED for every catalog together when system.data_classification.results has zero rows anywhere in the snapshot - that reads as the classification table itself being unavailable, not that every catalog scored zero.
-- healthy: status = OK; covered_pct at/above :warn_coverage_pct - field heuristic; a catalog with little sensitive data can still show a low share honestly.
-- investigate_if: status = WARN, covered_pct below :warn_coverage_pct - a real coverage gap, either a catalog never enabled for classification or one whose scans have not caught up; status = NOT_ASSESSED means coverage could not be measured at all (read not_assessed_reason), never a zero.
-- actions: 1) confirm the catalog has BOTH the data-classification feature and the system.data_classification schema enabled - Databricks: enable Unity Catalog automatic data classification for the catalog (free); 2) for a catalog just enabled, wait - scans run on serverless compute and complete asynchronously, within about 24h for a new object (free); 3) for a catalog that stays low after that, check serverless availability for the workspace's region, since classification scans need serverless compute (config).
-- next: access_data_classification_inventory (see what was found, table by table), access_classified_unmasked (see which classified columns are unmasked), access_pii_outside_tables (columns that look sensitive but carry no tag)
-- not_assessed_reasons: classification_table_empty: no catalog anywhere in the snapshot has a data-classification result yet, so coverage cannot be measured for any of them
-- caveats: system.data_classification.results only records a row for a column it actually classified with a class_tag - there is no separate "scanned, nothing found" marker in this system table, so a table that was scanned and came back clean is indistinguishable here from a table that was never scanned; covered_pct is how much of the catalog has ANY recorded classification activity, a floor, not proof every uncovered table is unscanned. Public Preview, 13-month retention, regional, and it requires BOTH the data-classification feature AND the separate system.data_classification schema enabled - enabling one does not enable the other. tables_total excludes VIEW rows (a view has no storage of its own to classify) and the 'system' catalog / 'information_schema' schema (Databricks does not run classification against its own system tables), the same exclusion access_dead_table_candidates uses. information_schema is privilege-aware, so a catalog you cannot see at all is simply absent from this list, not proven zero. status is NOT_ASSESSED for every catalog together, with not_assessed_reason = classification_table_empty, when system.data_classification.results has zero rows across the WHOLE snapshot (checked once, via a single COUNT(*) over system.data_classification.results, cross-joined onto every catalog row, so no per-catalog join is needed) - the same emptiness a never-enabled feature, a disabled system.data_classification schema, or a snapshot taken before any scan completed all produce, so this status cannot be told apart from those three causes by this query alone; once the source table has at least one row anywhere, every catalog's own covered_pct stands on its own, including a genuine 0%. tables_classified is deliberately bounded to rows in `classified` that INNER JOIN back onto `inventory` (a real, current, non-VIEW table in a non-excluded catalog/schema) - without that bound, a classified VIEW, a since-dropped table, or a row under the 'system' catalog would inflate tables_classified past tables_total and covered_pct above 100%. This query is new, not a copy of a vendored file; it reads two tables access_data_classification_inventory and storage_target_table_discovery already prove out in this library.
WITH inventory AS (
  -- one row per real, current non-VIEW table this account holds (dedup: information_schema.tables
  -- is already unique per table, DISTINCT here only protects the join below if that ever changes)
  SELECT DISTINCT table_catalog AS catalog_name, table_schema AS schema_name, table_name
  FROM system.information_schema.tables
  WHERE table_type <> 'VIEW'
    AND table_catalog <> 'system'
    AND table_schema <> 'information_schema'
),
catalog_tables AS (
  SELECT catalog_name,
         COUNT(*) AS tables_total
  FROM inventory
  GROUP BY catalog_name
),
classified AS (
  SELECT DISTINCT catalog_name, schema_name, table_name
  FROM system.data_classification.results
),
-- bound classification hits to tables that actually exist in the inventory today, so a
-- classified VIEW, a dropped table, or a table in an excluded catalog/schema never inflates
-- tables_classified past tables_total
classified_in_inventory AS (
  SELECT i.catalog_name, i.schema_name, i.table_name
  FROM inventory i
  INNER JOIN classified c
    ON  c.catalog_name = i.catalog_name
    AND c.schema_name  = i.schema_name
    AND c.table_name   = i.table_name
),
classified_by_catalog AS (
  SELECT catalog_name,
         COUNT(*) AS tables_classified
  FROM classified_in_inventory
  GROUP BY catalog_name
),
-- whole-snapshot check for "classification could not be measured at all": a single count over
-- the raw source table, independent of the inventory join above, so a results table that only
-- has hits on views / dropped tables / catalogs the snapshot principal cannot see still reads
-- as "the source has rows" rather than being mistaken for classification never having run
results_rows AS (
  SELECT COUNT(*) AS n_rows
  FROM system.data_classification.results
),
joined AS (
  SELECT ct.catalog_name,
         ct.tables_total,
         COALESCE(cc.tables_classified, 0) AS tables_classified
  FROM catalog_tables ct
  LEFT JOIN classified_by_catalog cc
    ON cc.catalog_name = ct.catalog_name
)
SELECT
  j.catalog_name,
  j.tables_total,
  j.tables_classified,
  -- NULL account-wide, not per catalog, when system.data_classification.results has zero rows
  -- anywhere in the whole snapshot
  CASE WHEN r.n_rows = 0 THEN NULL
       ELSE ROUND(100.0 * j.tables_classified / NULLIF(j.tables_total, 0), 1)
  END AS covered_pct,
  CASE
    WHEN r.n_rows = 0 THEN 'NOT_ASSESSED'
    WHEN 100.0 * j.tables_classified / NULLIF(j.tables_total, 0) < :warn_coverage_pct THEN 'WARN'
    ELSE 'OK'
  END AS status,
  CASE WHEN r.n_rows = 0 THEN 'classification_table_empty'
       ELSE NULL
  END AS not_assessed_reason
FROM joined j
CROSS JOIN results_rows r
ORDER BY CASE status WHEN 'NOT_ASSESSED' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
         covered_pct ASC NULLS LAST,
         catalog_name
