{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:storage', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/storage/storage_po_coverage.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH latest AS (
  SELECT catalog_name, schema_name, table_name, table_id, active_bytes, predictive_optimization_enabled
  FROM {{ source('system_storage', 'table_metrics_history') }}
  WHERE snapshot_date >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND snapshot_date < {{ audit_today() }}
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
  QUALIFY ROW_NUMBER() OVER (ORDER BY t.active_bytes DESC) <= {{ param('storage_po_coverage', 'top_n', 50) }}
),
combined AS (
  SELECT * FROM by_catalog
  UNION ALL
  SELECT * FROM top_tables
)
SELECT c.*,
  CASE
    WHEN c.section = 'largest_table_without_po' THEN 'WARN'
    WHEN c.pct_bytes_without_po >= {{ param('storage_po_coverage', 'crit_pct_bytes_without_po', 50) }} THEN 'CRITICAL'
    WHEN c.pct_bytes_without_po >= {{ param('storage_po_coverage', 'warn_pct_bytes_without_po', 20) }} THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM combined c
ORDER BY CASE c.section WHEN 'catalog_summary' THEN 0 ELSE 1 END,
         CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         c.pct_bytes_without_po DESC NULLS LAST,
         c.active_bytes_of_table DESC NULLS LAST
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
