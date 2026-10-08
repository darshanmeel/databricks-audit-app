{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/storage/storage_small_files.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH latest AS (
  SELECT catalog_name, schema_name, table_name, table_id, table_type, table_owner,
         snapshot_date, active_bytes, active_files
  FROM {{ source('system_storage', 'table_metrics_history') }}
  WHERE snapshot_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND snapshot_date < {{ audit_today() }}
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
    WHEN l.active_files > {{ param('storage_small_files', 'warn_min_files', 1000) }}
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < {{ param('storage_small_files', 'crit_max_avg_mb', 8) }} THEN 'CRITICAL'
    WHEN l.active_files > {{ param('storage_small_files', 'warn_min_files', 1000) }}
         AND l.active_bytes / NULLIF(l.active_files, 0) / 1024.0 / 1024.0 < {{ param('storage_small_files', 'warn_max_avg_mb', 32) }} THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM latest l
WHERE l.active_files > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, l.active_files DESC
LIMIT {{ param('storage_small_files', 'top_n', 100000) }}
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
