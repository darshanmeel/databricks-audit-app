{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/storage/storage_growth.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH snaps AS (
  SELECT table_id, catalog_name, schema_name, table_name, table_owner, table_dropped_time,
         snapshot_date, active_bytes
  FROM {{ source('system_storage', 'table_metrics_history') }}
  WHERE snapshot_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND snapshot_date < {{ audit_today() }}
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
  (l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS)
    AS dropped_in_window,
  CASE
    WHEN l.table_dropped_time IS NOT NULL AND l.table_dropped_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS THEN 'WARN'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= {{ param('storage_growth', 'min_growth_bytes', 1073741824) }}
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= {{ param('storage_growth', 'crit_growth_pct', 50) }}
      THEN 'CRITICAL'
    WHEN f.first_bytes > 0 AND (l.last_bytes - f.first_bytes) >= {{ param('storage_growth', 'min_growth_bytes', 1073741824) }}
      AND (l.last_bytes - f.first_bytes) * 100.0 / f.first_bytes >= {{ param('storage_growth', 'warn_growth_pct', 20) }}
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM first_snap f
JOIN last_snap l ON l.table_id = f.table_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         growth_bytes DESC NULLS LAST
LIMIT {{ param('storage_growth', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
