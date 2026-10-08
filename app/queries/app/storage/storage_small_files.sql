-- query_id: storage_small_files
-- title: Tables with too many small files - slow scans, inflated listing cost
-- domain: storage   tier: standard
-- reads: system.storage.table_metrics_history
-- requires: SELECT on system.storage; system.storage.table_metrics_history must be enabled for
--   this metastore - if it is not, this check errors and reads not assessed (system table not
--   enabled) until it is turned on.
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window used to find each table's latest snapshot (a
--   table with no snapshot in the window is not scored); :warn_min_files (default 1000) active
--   file count above which a table is scored at all; :warn_max_avg_mb (default 32) average file
--   size in MB under which WARN fires (with :warn_min_files also met); :crit_max_avg_mb (default 8)
--   average file size in MB under which CRITICAL fires (with :warn_min_files also met); :top_n
--   (default 100000) row cap - far above any account's real table count, ordered worst (most
--   files) first so a cut row is always the least urgent
-- confidence: needs_confirmation
-- confidence_note: column names (active_bytes, active_files, table_owner, table_dropped_time,
--   table_type) come from a live DESCRIBE of this table on an account that has it enabled, but
--   this query has not yet run live end to end. Confirm on your account that a table you know has
--   many small files (frequent small-batch or streaming writes with no compaction) shows CRITICAL
--   or WARN here, and that avg_file_size_mb matches DESCRIBE DETAIL's own numFiles/sizeInBytes.
-- read_this: One row = one table (catalog.schema.table), at its latest snapshot inside
--   :period_days days. avg_file_size_mb = active_bytes / active_files. status flags a table with
--   more than :warn_min_files active files AND a small avg_file_size_mb - many small files add
--   open/seek overhead per read and bloat the file listing even when total size is small.
-- healthy: OK - active_files at/below :warn_min_files, or avg_file_size_mb at/above
--   :warn_max_avg_mb.
-- investigate_if: CRITICAL - more than :warn_min_files active files and avg_file_size_mb under
--   :crit_max_avg_mb. WARN - more than :warn_min_files active files and avg_file_size_mb under
--   :warn_max_avg_mb (and not already CRITICAL).
-- actions: 1) run OPTIMIZE on the table to compact small files (spend); 2) turn on predictive
--   optimization at the table or catalog level so Databricks compacts automatically going forward
--   (config - see storage_po_coverage for tables missing it); 3) for a table with a high write
--   rate, consider liquid clustering instead of a fixed partitioning scheme (redesign/spend).
-- next: storage_po_coverage (is predictive optimization already off for this table),
--   storage_growth (is this table also growing fast - more small files coming),
--   po_failure_reasons (if predictive optimization is on but failing to compact it)
-- caveats: One row per table_id, its single latest snapshot_date within :period_days days - a
--   table with no snapshot in that window (dropped, or metrics not yet captured for it) is
--   silently absent; widen :period_days if a known table is missing. Bytes and files only; this
--   carries no cost figure (table_metrics_history has none) - pair with storage_growth for the
--   same table's size trend. A table dropped before the window's end (table_dropped_time IS NOT
--   NULL) is excluded outright, not scored.
WITH latest AS (
  SELECT catalog_name, schema_name, table_name, table_id, table_type, table_owner,
         snapshot_date, active_bytes, active_files
  FROM system.storage.table_metrics_history
  WHERE snapshot_date >= current_date() - INTERVAL :period_days DAYS
    AND snapshot_date < current_date()
    AND table_dropped_time IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
)
SELECT
  l.catalog_name,
  l.schema_name,
  l.table_name,
  CONCAT(l.catalog_name, '.', l.schema_name, '.', l.table_name) AS full_name,
  l.table_id,
  l.table_type,
  l.table_owner,
  l.snapshot_date,
  l.active_bytes,
  l.active_files,
  ROUND(l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0, 2) AS avg_file_size_mb,
  CASE
    WHEN l.active_files > :warn_min_files
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < :crit_max_avg_mb THEN 'CRITICAL'
    WHEN l.active_files > :warn_min_files
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < :warn_max_avg_mb THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM latest l
WHERE l.active_files > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, l.active_files DESC
LIMIT :top_n
