-- query_id: storage_growth
-- title: Fastest-growing tables, tables dropped in the window, tables with no owner
-- domain: storage   tier: standard
-- reads: system.storage.table_metrics_history
-- requires: SELECT on system.storage; system.storage.table_metrics_history must be enabled for
--   this metastore - if it is not, this check errors and reads not assessed (system table not
--   enabled) until it is turned on.
-- empty_if: schema_not_enabled
-- params: :period_days (default 30) rolling window compared start to end; :warn_growth_pct
--   (default 20) percent growth over the window that flags WARN; :crit_growth_pct (default 50)
--   percent that flags CRITICAL; :min_growth_bytes (default 1073741824) growth_bytes below which a
--   percent-growth verdict never fires (default = 1 GiB) - a table going from 1 KB to 2 KB is
--   technically "+100%" but too small to matter; :top_n (default 100000) row cap - far above any account's real
--   table count, ordered worst (fastest-growing) first so a cut row is always the least urgent
-- confidence: needs_confirmation
-- confidence_note: active_bytes, table_owner and table_dropped_time come from a live DESCRIBE of
--   this table on an account that has it enabled, but this query has not yet run live end to end.
--   Confirm on your account that a table you know grew a lot this month shows a matching
--   growth_pct, and that a recently dropped table shows dropped_in_window=TRUE.
-- read_this: One row = one table with at least one snapshot in the window. growth_bytes/growth_pct
--   compare its first and last snapshot in the window - bytes only, no dollars (table_metrics_
--   history has no cost figure to attach; pair with storage_po_coverage/storage_small_files for a
--   fuller picture, or cost_monthly_actuals for account-wide storage spend). dropped_in_window and
--   no_owner are plain flags, independent of the growth verdict.
-- healthy: OK - growth_pct below :warn_growth_pct, or growth_bytes below :min_growth_bytes, and not
--   dropped in the window.
-- investigate_if: WARN - growth_pct at/above :warn_growth_pct on at least :min_growth_bytes of real
--   growth, or the table was dropped in the window (a drop worth a look, not a failure - flagged
--   rather than hidden, at any size). CRITICAL - growth_pct at/above :crit_growth_pct on at least
--   :min_growth_bytes.
-- actions: 1) for fast growers, check retention/partitioning and whether old data should be
--   archived or deleted (free/config); 2) for no_owner tables, set a table owner so someone is
--   accountable for its lifecycle (config); 3) for a table dropped in the window, confirm the drop
--   was intentional (free).
-- next: storage_small_files (is a fast grower also fragmenting), storage_po_coverage (is it
--   missing predictive optimization), po_failure_reasons (if predictive optimization is on but not
--   keeping up with its growth)
-- caveats: growth_pct is NULL when first_bytes is 0 (a table with no bytes at the start of the
--   window - division avoided, not an error); such a table still reads its plain growth_bytes and
--   flags are then judged on dropped_in_window alone. A table with only one snapshot in the window
--   has first_snapshot_date = last_snapshot_date and growth_bytes = 0. table_dropped_time is
--   compared to the window's own start, not to :period_days from each table's own first_snapshot.
--   catalog/schema/table name, table_owner and table_dropped_time come from the table's LAST
--   snapshot in the window, not its first - a table dropped mid-window carries the drop time that
--   actually applies, and a rename/owner change mid-window shows the current name/owner, not a
--   stale one from the start of the window.
WITH snaps AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date, active_bytes
  FROM system.storage.table_metrics_history
  WHERE snapshot_date >= current_date() - INTERVAL :period_days DAYS
    AND snapshot_date < current_date()
),
first_snap AS (
  SELECT table_id, snapshot_date AS first_snapshot_date, active_bytes AS first_bytes
  FROM snaps
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date ASC) = 1
),
last_snap AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date AS last_snapshot_date, active_bytes AS last_bytes
  FROM snaps
  QUALIFY ROW_NUMBER() OVER (PARTITION BY table_id ORDER BY snapshot_date DESC) = 1
)
SELECT
  l.table_id,
  l.catalog_name,
  l.schema_name,
  l.table_name,
  CONCAT(l.catalog_name, '.', l.schema_name, '.', l.table_name) AS full_name,
  l.table_owner,
  (l.table_owner IS NULL OR l.table_owner = '') AS no_owner,
  f.first_snapshot_date,
  f.first_bytes,
  l.last_snapshot_date,
  l.last_bytes,
  l.last_bytes - f.first_bytes AS growth_bytes,
  ROUND((l.last_bytes - f.first_bytes) * 100.0 / NULLIF(f.first_bytes, 0), 1) AS growth_pct,
  (l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= current_date() - INTERVAL :period_days DAYS)
    AS dropped_in_window,
  CASE
    WHEN l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= current_date() - INTERVAL :period_days DAYS THEN 'WARN'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= :min_growth_bytes
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= :crit_growth_pct
      THEN 'CRITICAL'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= :min_growth_bytes
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= :warn_growth_pct
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM first_snap f
JOIN last_snap l ON l.table_id = f.table_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         growth_bytes DESC NULLS LAST
LIMIT :top_n
