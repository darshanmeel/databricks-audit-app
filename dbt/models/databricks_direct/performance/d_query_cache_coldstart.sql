{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:performance', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/performance/query_cache_coldstart.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM {{ source('system_compute', 'warehouses') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
agg AS (
  SELECT date(start_time) AS day, workspace_id, compute.type AS compute_type, compute.warehouse_id AS warehouse_id,
         COUNT(*) AS query_count,
         SUM(CASE WHEN from_result_cache THEN 1 ELSE 0 END) AS result_cache_hit_count,
         -- rows this average is actually over: scanned real bytes and were not themselves a cache hit.
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false) THEN 1 ELSE 0 END) AS scanned_query_count,
         AVG(read_io_cache_percent) AS read_io_cache_percent_avg,
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                  THEN read_io_cache_percent * read_bytes END) AS weighted_num,
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                  THEN read_bytes END) AS weighted_den,
         SUM(CASE WHEN read_bytes > 0 AND NOT COALESCE(from_result_cache, false)
                   AND read_io_cache_percent < {{ param('query_cache_coldstart', 'warn_io_cache_pct', 50) }} THEN 1 ELSE 0 END) AS low_io_cache_count,
         SUM(waiting_for_compute_duration_ms) AS waiting_for_compute_ms_sum,
         SUM(compilation_duration_ms)         AS compilation_duration_ms_sum
  FROM {{ source('system_query', 'history') }}
  WHERE start_time >= {{ audit_today() }} - INTERVAL {{ w }} DAYS
    AND start_time < {{ audit_today() }}
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id
)
SELECT a.day, a.workspace_id, a.compute_type, a.warehouse_id, lw.warehouse_name,
       a.query_count, a.result_cache_hit_count, a.scanned_query_count,
       ROUND(a.read_io_cache_percent_avg, 1)                AS read_io_cache_percent_avg,
       ROUND(a.weighted_num / NULLIF(a.weighted_den, 0), 1) AS read_io_cache_percent_weighted_avg,
       a.low_io_cache_count, a.waiting_for_compute_ms_sum, a.compilation_duration_ms_sum,
       -- status: worst-first band on the read_bytes-weighted IO-cache hit rate (field heuristic;
       -- {{ param('query_cache_coldstart', 'warn_io_cache_pct', 50) }} / {{ param('query_cache_coldstart', 'crit_io_cache_pct', 20) }}). NOT_ASSESSED when nothing in the group scanned
       -- data - the old plain average could dilute such a group into a false OK.
       CASE
         WHEN a.scanned_query_count = 0 THEN 'NOT_ASSESSED'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < {{ param('query_cache_coldstart', 'crit_io_cache_pct', 20) }} THEN 'CRITICAL'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < {{ param('query_cache_coldstart', 'warn_io_cache_pct', 50) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
LEFT JOIN latest_wh lw ON lw.warehouse_id = a.warehouse_id
ORDER BY read_io_cache_percent_weighted_avg ASC NULLS LAST
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
