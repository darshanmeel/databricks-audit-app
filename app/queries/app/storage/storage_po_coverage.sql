-- query_id: storage_po_coverage
-- title: Predictive optimization coverage by catalog, and the largest tables missing it
-- domain: storage   tier: standard
-- reads: system.storage.table_metrics_history
-- requires: SELECT on system.storage; system.storage.table_metrics_history must be enabled for
--   this metastore - if it is not, this check errors and reads not assessed (system table not
--   enabled) until it is turned on.
-- empty_if: schema_not_enabled, po_not_enabled
-- params: :period_days (default 30) rolling window used to find each table's latest snapshot;
--   :warn_pct_bytes_without_po (default 20) percent of a catalog's bytes without predictive
--   optimization that flags WARN; :crit_pct_bytes_without_po (default 50) percent that flags
--   CRITICAL; :top_n (default 50) largest tables without predictive optimization to list
--   individually - a to-do list, not every table missing it
-- confidence: needs_confirmation
-- confidence_note: predictive_optimization_enabled and active_bytes come from a live DESCRIBE of
--   this table on an account that has it enabled, but this query has not yet run live end to end.
--   Confirm on your account that a catalog you turned predictive optimization on for reads 0%
--   bytes_without_po, and that a catalog you never touched reads close to 100%.
-- read_this: Two sections in one result, told apart by `section`. catalog_summary - one row per
--   catalog: tables_total/tables_without_po/bytes_total/bytes_without_po and their percentages.
--   largest_table_without_po - up to :top_n tables (by active_bytes) with predictive optimization
--   off, biggest first; a to-do list, always WARN on its own regardless of its catalog's status -
--   check that catalog's own catalog_summary row for the account-wide severity.
-- healthy: OK (catalog_summary only) - pct_bytes_without_po below :warn_pct_bytes_without_po.
-- investigate_if: CRITICAL (catalog_summary) - pct_bytes_without_po at/above
--   :crit_pct_bytes_without_po. WARN (catalog_summary) - at/above :warn_pct_bytes_without_po.
--   largest_table_without_po rows are always WARN - work through them regardless of the catalog's
--   own status.
-- actions: 1) turn on predictive optimization at the catalog level (ALTER CATALOG ... SET
--   PREDICTIVE OPTIMIZATION ON) so every table in it is covered going forward (config); 2) or turn
--   it on per table for just the largest offenders in largest_table_without_po first (config);
--   3) after turning it on, check po_failure_reasons for that table in case operations are failing
--   rather than simply never having run (free).
-- next: po_failure_reasons (is predictive optimization on but failing for these tables),
--   storage_small_files (are the largest tables without it also fragmented), storage_growth (are
--   they also growing fast)
-- caveats: catalog_summary's percentages are over tables with a snapshot inside :period_days days
--   only - a catalog with no recent snapshot for any of its tables is silently absent, not a real
--   0%. largest_table_without_po rows repeat none of catalog_summary's own columns (NULL there) and
--   vice versa - never sum pct_bytes_without_po or active_bytes_of_table across sections, only
--   within one. A table dropped before the window's end is excluded outright from both sections.
WITH latest AS (
  SELECT catalog_name, schema_name, table_name, table_id, active_bytes, predictive_optimization_enabled
  FROM system.storage.table_metrics_history
  WHERE snapshot_date >= current_date() - INTERVAL :period_days DAYS
    AND snapshot_date < current_date()
    AND table_dropped_time IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
),
by_catalog AS (
  SELECT
    'catalog_summary' AS section,
    catalog_name,
    CAST(NULL AS STRING) AS schema_name,
    CAST(NULL AS STRING) AS table_id,
    CAST(NULL AS STRING) AS table_name,
    CAST(NULL AS STRING) AS full_name,
    COUNT(*) AS tables_total,
    SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN 1 ELSE 0 END) AS tables_without_po,
    ROUND(SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1)
      AS pct_tables_without_po,
    SUM(active_bytes) AS bytes_total,
    SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN active_bytes ELSE 0 END) AS bytes_without_po,
    ROUND(SUM(CASE WHEN predictive_optimization_enabled = FALSE THEN active_bytes ELSE 0 END) * 100.0
          / NULLIF(SUM(active_bytes), 0), 1) AS pct_bytes_without_po,
    CAST(NULL AS BIGINT) AS active_bytes_of_table
  FROM latest
  GROUP BY catalog_name
),
top_tables AS (
  SELECT
    'largest_table_without_po' AS section,
    t.catalog_name, t.schema_name, t.table_id, t.table_name,
    CONCAT(t.catalog_name, '.', t.schema_name, '.', t.table_name) AS full_name,
    CAST(NULL AS BIGINT) AS tables_total,
    CAST(NULL AS BIGINT) AS tables_without_po,
    CAST(NULL AS DOUBLE) AS pct_tables_without_po,
    CAST(NULL AS BIGINT) AS bytes_total,
    CAST(NULL AS BIGINT) AS bytes_without_po,
    CAST(NULL AS DOUBLE) AS pct_bytes_without_po,
    t.active_bytes AS active_bytes_of_table
  FROM latest t
  WHERE t.predictive_optimization_enabled = FALSE
  QUALIFY ROW_NUMBER() OVER (ORDER BY t.active_bytes DESC) <= :top_n
),
combined AS (
  SELECT * FROM by_catalog
  UNION ALL
  SELECT * FROM top_tables
)
SELECT c.*,
  CASE
    WHEN c.section = 'largest_table_without_po' THEN 'WARN'
    WHEN c.pct_bytes_without_po >= :crit_pct_bytes_without_po THEN 'CRITICAL'
    WHEN c.pct_bytes_without_po >= :warn_pct_bytes_without_po THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM combined c
ORDER BY CASE c.section WHEN 'catalog_summary' THEN 0 ELSE 1 END,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         c.pct_bytes_without_po DESC NULLS LAST,
         c.active_bytes_of_table DESC NULLS LAST
