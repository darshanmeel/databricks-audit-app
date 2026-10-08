-- query_id: query_cache_coldstart
-- title: Result-cache hit rate and IO-cache cold-start latency
-- domain: performance   tier: standard
-- reads: system.query.history, system.compute.warehouses
-- requires: SELECT on system.query AND system.compute; GA (system.query.history is generally available)
-- empty_if: schema_not_enabled, preview_unavailable, compute_scope_gap
-- params: :period_days (default 30) rolling window in days; :warn_io_cache_pct (default 50) byte-weighted read_io_cache_percent below which a day+warehouse flags WARN, and the per-query low-cache threshold; :crit_io_cache_pct (default 20) byte-weighted read_io_cache_percent below which it flags CRITICAL
-- confidence: confirmed
-- confidence_note: Columns verified against system.query.history in a live workspace.
-- read_this: One row = a day + warehouse whose queries were checked for cache reuse. The column that drives status is read_io_cache_percent_weighted_avg (a read_bytes-weighted average over statements that actually scanned data) - not the plain, unweighted read_io_cache_percent_avg kept alongside it for reference. waiting_for_compute_ms_sum (cumulative cold-start/provisioning wait) is a separate signal. scanned_query_count is how many of query_count actually scanned bytes; a day with a low weighted average and a high low_io_cache_count is spending compute re-reading data it should be caching.
-- healthy: read_io_cache_percent_weighted_avg at/above :warn_io_cache_pct (field heuristic - tune :warn_io_cache_pct for your account).
-- investigate_if: read_io_cache_percent_weighted_avg below :warn_io_cache_pct (WARN) or below :crit_io_cache_pct (CRITICAL) - field heuristic.
-- actions: 1) re-run a representative query back-to-back to confirm it is actually cold rather than just infrequently reused (free); 2) pin hot tables to a warehouse that stays warm between runs, or extend the warehouse's auto-stop timeout to avoid cold starts between queries (config); 3) move latency-sensitive workloads to a warehouse that never idles down, such as a dedicated always-on warehouse (spend).
-- next: query_queuing_waits (if waiting_for_compute_ms_sum is also high), query_local_spillage (if the same warehouse also spills)
-- caveats: There are two distinct cache signals here and they are NOT one combined percentage: from_result_cache is a boolean (did this exact query hit the result cache) while read_io_cache_percent is the share of scanned bytes served from disk/IO cache - do not average or add them together. read_io_cache_percent_avg is a PLAIN unweighted average across every statement in the group, including non-scan statements that legitimately report 0 - it dilutes toward 0 for reasons unrelated to real cache-cold-start pain and is kept only for reference; read_io_cache_percent_weighted_avg is a read_bytes-weighted average over statements with read_bytes > 0 that were not themselves a result-cache hit, and is what status actually bands on. A day+warehouse where nothing scanned data (scanned_query_count = 0) reads NOT_ASSESSED, never OK, and read_io_cache_percent_weighted_avg is NULL. waiting_for_compute_duration_ms is cold-start latency from warehouse provisioning; compilation_duration_ms is metadata/optimizer-bound time - the two measure different things and should not be conflated. warehouse_name is the latest name for warehouse_id from system.compute.warehouses (SCD2, latest row by change_time) and is NULL for a deleted warehouse or a NULL warehouse_id (serverless). This table is regional, so a workspace with warehouses in multiple regions needs one run per region to see the full picture.
-- system.query.history only records queries run on SQL warehouses or serverless compute; queries on classic all-purpose and job clusters are never captured, so these cache metrics omit any classic-cluster workload entirely rather than showing it as zero.
WITH latest_wh AS (
  SELECT warehouse_id, warehouse_name
  FROM system.compute.warehouses
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
                   AND read_io_cache_percent < :warn_io_cache_pct THEN 1 ELSE 0 END) AS low_io_cache_count,
         SUM(waiting_for_compute_duration_ms) AS waiting_for_compute_ms_sum,
         SUM(compilation_duration_ms)         AS compilation_duration_ms_sum
  FROM system.query.history
  WHERE start_time >= current_date() - INTERVAL :period_days DAYS
    AND start_time < current_date()
  GROUP BY date(start_time), workspace_id, compute.type, compute.warehouse_id
)
SELECT a.day, a.workspace_id, a.compute_type, a.warehouse_id, lw.warehouse_name,
       a.query_count, a.result_cache_hit_count, a.scanned_query_count,
       ROUND(a.read_io_cache_percent_avg, 1)                AS read_io_cache_percent_avg,
       ROUND(a.weighted_num / NULLIF(a.weighted_den, 0), 1) AS read_io_cache_percent_weighted_avg,
       a.low_io_cache_count, a.waiting_for_compute_ms_sum, a.compilation_duration_ms_sum,
       -- status: worst-first band on the read_bytes-weighted IO-cache hit rate (field heuristic;
       -- :warn_io_cache_pct / :crit_io_cache_pct). NOT_ASSESSED when nothing in the group scanned
       -- data - the old plain average could dilute such a group into a false OK.
       CASE
         WHEN a.scanned_query_count = 0 THEN 'NOT_ASSESSED'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < :crit_io_cache_pct THEN 'CRITICAL'
         WHEN a.weighted_num / NULLIF(a.weighted_den, 0) < :warn_io_cache_pct THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM agg a
LEFT JOIN latest_wh lw ON lw.warehouse_id = a.warehouse_id
ORDER BY read_io_cache_percent_weighted_avg ASC NULLS LAST
