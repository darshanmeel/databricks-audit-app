{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:governance_access', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/governance_access/access_classification_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH inventory AS (
  -- one row per real, current non-VIEW table this account holds (dedup: information_schema.tables
  -- is already unique per table, DISTINCT here only protects the join below if that ever changes)
  SELECT DISTINCT table_catalog AS catalog_name, table_schema AS schema_name, table_name
  FROM {{ source('system_information_schema', 'tables') }}
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
  FROM {{ source('system_data_classification', 'results') }}
),
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
results_rows AS (
  SELECT COUNT(*) AS n_rows
  FROM {{ source('system_data_classification', 'results') }}
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
    WHEN 100.0 * j.tables_classified / NULLIF(j.tables_total, 0) < {{ param('access_classification_coverage', 'warn_coverage_pct', 80) }} THEN 'WARN'
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
) q
