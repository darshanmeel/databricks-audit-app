-- generated from dbt/models/databricks_direct/storage/d_storage_small_files.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_small_files.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest AS (
  SELECT catalog_name, schema_name, table_name, table_id, table_type, table_owner,
         snapshot_date, active_bytes, active_files
  FROM `system`.`storage`.`table_metrics_history`
  WHERE snapshot_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND snapshot_date < __AS_OF_DATE__
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
    WHEN l.active_files > 1000
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < 8 THEN 'CRITICAL'
    WHEN l.active_files > 1000
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < 32 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM latest l
WHERE l.active_files > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, l.active_files DESC
LIMIT 100000
) q
