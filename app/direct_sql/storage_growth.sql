-- generated from dbt/models/databricks_direct/storage/d_storage_growth.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/storage/storage_growth.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snaps AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date, active_bytes
  FROM `system`.`storage`.`table_metrics_history`
  WHERE snapshot_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND snapshot_date < __AS_OF_DATE__
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
  (l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS)
    AS dropped_in_window,
  CASE
    WHEN l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS THEN 'WARN'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= 1073741824
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= 50
      THEN 'CRITICAL'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= 1073741824
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= 20
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM first_snap f
JOIN last_snap l ON l.table_id = f.table_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         growth_bytes DESC NULLS LAST
LIMIT 100000
) q
