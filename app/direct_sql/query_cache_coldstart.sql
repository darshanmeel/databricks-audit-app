-- generated from dbt/models/databricks_direct/performance/d_query_cache_coldstart.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/performance/query_cache_coldstart.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
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
                   AND read_io_cache_percent < 50 THEN 1 ELSE 0 END) AS low_io_cache_count,
         SUM(waiting_for_compute_duration_ms) AS waiting_for_compute_ms_sum,
         SUM(compilation_duration_ms)         AS compilation_duration_ms_sum
  FROM `system`.`query`.`history`
  WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND start_time < __AS_OF_DATE__
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id
)
SELECT a.day, a.workspace_id, a.compute_type, a.warehouse_id, lw.warehouse_name,
       a.query_count, a.result_cache_hit_count, a.scanned_query_count,
       ROUND(a.read_io_cache_percent_avg, 1)                AS read_io_cache_percent_avg,
       ROUND(a.weighted_num / NULLIF(a.weighted_den, 0), 1) AS read_io_cache_percent_weighted_avg,
       a.low_io_cache_count, a.waiting_for_compute_ms_sum, a.compilation_duration_ms_sum,
       -- status: worst-first band on the read_bytes-weighted IO-cache hit rate (field heuristic;
       -- 50 / 20). NOT_ASSESSED when nothing in the group scanned
       -- data - the old plain average could dilute such a group into a false OK.
       CASE
         WHEN a.scanned_query_count = 0 THEN 'NOT_ASSESSED'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < 20 THEN 'CRITICAL'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
LEFT JOIN latest_wh lw ON lw.warehouse_id = a.warehouse_id
ORDER BY read_io_cache_percent_weighted_avg ASC NULLS LAST
) q
