-- query_id: compute_warehouse_cache_reuse
-- title: SQL warehouse result-cache reuse - does staying warm actually pay off
-- domain: compute   tier: standard
-- reads: system.query.history, system.compute.warehouses
-- requires: SELECT on system.query and system.compute; GA
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :min_queries_for_verdict (default 10)
--   queries in the window below which a warehouse reads OK regardless of cache share - too little
--   signal; :warn_low_cache_share_pct (default 5) from_result_cache share below which a warehouse
--   counts as low
-- confidence: needs_confirmation
-- confidence_note: from_result_cache and read_io_cache_percent are documented system.query.history
--   columns this app has not read before. Confirm on your own account: 1) a warehouse you know serves
--   many repeat dashboards/queries shows a high from_result_cache_pct; 2) a warehouse with a long
--   auto-stop and genuinely one-off queries shows a low share here.
-- read_this: One row = one SQL warehouse over the window. from_result_cache_pct is the share of its
--   queries Databricks served straight from the result cache. repeated_statement_pct is the share of
--   its queries whose exact statement text (hashed) also ran at least once more on the same warehouse
--   - a second, independent signal for "this warehouse serves repeat work" that does not depend on
--   the result cache actually firing. median_gap_minutes is the typical time between one query
--   finishing and the next starting on that warehouse - a long gap next to a long auto_stop_minutes is
--   spending idle minutes between infrequent queries.
-- healthy: status = OK - fewer than :min_queries_for_verdict queries in the window (too little
--   signal), or from_result_cache_pct at/above :warn_low_cache_share_pct.
-- investigate_if: status = WARN - from_result_cache_pct below :warn_low_cache_share_pct with
--   auto_stop_minutes ABOVE the warehouse's own type minimum (serverless: 5 minutes; classic/pro:
--   10 minutes) - there is room to lower it and the warehouse is not using the repeat-query benefit
--   staying warm would buy. A warehouse already AT its type's minimum never reads WARN on
--   auto-stop alone, however low its cache share - there is nothing left to lower. Never CRITICAL:
--   low cache reuse is a heuristic (a unique-query workload always has a low share, not a config
--   mistake), not a confirmed problem.
-- actions: 1) lower auto_stop_minutes to the type's minimum (serverless: 5 minutes in the UI, 1 via
--   the API; classic/pro: 10 minutes) - a low cache-reuse warehouse loses little from a shorter wait
--   (free); 2) if repeated_statement_pct is high but from_result_cache_pct is low, check for a
--   changing filter or timestamp in the statement text that busts the cache on otherwise-identical
--   queries (free) - a warehouse already at its type's floor with this pattern gets no cheaper from
--   auto-stop, so this is the one action left for it.
-- next: compute_warehouse_config_posture (see auto_stop_minutes and whether it is already flagged),
--   compute_warehouse_idle_minutes (this warehouse's measured idle minutes and dollars over the same
--   window), compute_warehouse_autostop_churn (how often it actually auto-stops)
-- caveats: from_result_cache and read_io_cache_percent are read directly from system.query.history
--   with no further attribution - a query can be served from cache for reasons unrelated to this
--   warehouse's own auto-stop (a dashboard querying an unchanged table, for instance). This is a
--   correlation between a low cache-reuse rate and a long auto-stop, not proof the auto-stop caused
--   the low rate. repeated_statement_pct counts a statement as a duplicate by its exact SHA-256 text
--   hash - two statements that differ only by a literal (a date filter, an id) are never matched, so
--   this understates true repeat-workload volume; from_result_cache_pct is Databricks' own judgment
--   and does not have that limitation. No dollar figure is computed here - compute_warehouse_idle_
--   minutes already prices this warehouse's idle time; duplicating that estimate here would double-
--   count it.
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
q AS (
  SELECT h.workspace_id, h.compute.warehouse_id AS warehouse_id, h.start_time,
         h.from_result_cache, h.read_io_cache_percent,
         sha2(COALESCE(h.statement_text, ''), 256) AS statement_hash,
         unix_timestamp(h.start_time)
           - unix_timestamp(LAG(h.start_time) OVER (PARTITION BY h.compute.warehouse_id ORDER BY h.start_time))
           AS gap_seconds
  FROM system.query.history h
  WHERE h.compute.warehouse_id IS NOT NULL
    AND h.start_time >= date_sub(current_date(), :period_days)
    AND h.start_time < current_date()
),
stmt_counts AS (
  SELECT warehouse_id, statement_hash, COUNT(*) AS n
  FROM q
  GROUP BY warehouse_id, statement_hash
),
repeated_agg AS (
  SELECT warehouse_id, SUM(CASE WHEN n > 1 THEN n ELSE 0 END) AS repeated_query_rows
  FROM stmt_counts
  GROUP BY warehouse_id
),
agg AS (
  SELECT warehouse_id,
         COUNT(*) AS queries,
         SUM(CASE WHEN from_result_cache THEN 1 ELSE 0 END) AS from_cache_queries,
         AVG(read_io_cache_percent) AS avg_read_io_cache_percent,
         percentile(gap_seconds, 0.5) AS median_gap_seconds
  FROM q
  GROUP BY warehouse_id
)
SELECT
  a.warehouse_id,
  w.warehouse_name,
  CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
       WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
       WHEN upper(w.warehouse_type) = 'CLASSIC'     THEN 'classic'
  END AS warehouse_kind,
  w.auto_stop_minutes,
  a.queries,
  ROUND(a.from_cache_queries * 100.0 / a.queries, 1) AS from_result_cache_pct,
  ROUND(a.avg_read_io_cache_percent, 1) AS avg_read_io_cache_percent,
  ROUND(COALESCE(r.repeated_query_rows, 0) * 100.0 / a.queries, 1) AS repeated_statement_pct,
  ROUND(a.median_gap_seconds / 60.0, 1) AS median_gap_minutes,
  CASE
    WHEN a.queries < :min_queries_for_verdict THEN 'OK'
    -- Warn only when auto-stop sits ABOVE this warehouse's own type minimum -- 17 of 19 WARN
    -- warehouses on the fixture were already at the pro/classic floor (10 min) with nothing left to
    -- lower, so a fixed :warn_long_autostop_minutes threshold flagged them for a fix that does not
    -- exist. A type this query does not recognise (warehouse_kind NULL) never warns on auto-stop
    -- alone, rather than guess a floor for it.
    WHEN (a.from_cache_queries * 100.0 / a.queries) < :warn_low_cache_share_pct
         AND CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN w.auto_stop_minutes > 5
                  WHEN upper(w.warehouse_type) IN ('PRO', 'CLASSIC') THEN w.auto_stop_minutes > 10
                  ELSE FALSE END
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM agg a
-- OUTER: a warehouse with queries but no compute.warehouses row (deleted before system tables
-- started recording) still gets a row here, same as compute_warehouse_idle_minutes' cfg.
LEFT JOIN wh w ON w.warehouse_id = a.warehouse_id
LEFT JOIN repeated_agg r ON r.warehouse_id = a.warehouse_id
WHERE a.queries > 0
ORDER BY CASE status WHEN 'WARN' THEN 0 ELSE 1 END, from_result_cache_pct ASC
