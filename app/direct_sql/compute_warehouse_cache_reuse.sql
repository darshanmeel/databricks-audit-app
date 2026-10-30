-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_cache_reuse.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_cache_reuse.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
q AS (
  SELECT h.workspace_id, h.compute.warehouse_id AS warehouse_id, h.start_time,
         h.from_result_cache, h.read_io_cache_percent,
         upper(COALESCE(h.statement_type, '')) = 'SELECT' AS is_select,
         -- no text, or text redacted for this reader, is no evidence of a repeat
         CASE WHEN trim(COALESCE(h.statement_text, '')) NOT IN ('', '<REDACTED>')
              THEN sha2(h.statement_text, 256) END AS statement_hash,
         -- idle time before this query: from the latest end of any earlier query, never below 0
         GREATEST(unix_timestamp(h.start_time)
           - unix_timestamp(MAX(COALESCE(h.end_time, h.update_time, h.start_time)) OVER (
               PARTITION BY h.compute.warehouse_id ORDER BY h.start_time
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)), 0) AS gap_seconds
  FROM `system`.`query`.`history` h
  WHERE h.compute.warehouse_id IS NOT NULL
    AND h.start_time >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__)
    AND h.start_time < __AS_OF_DATE__
),
stmt_counts AS (
  SELECT warehouse_id, statement_hash, COUNT(*) AS n
  FROM q
  WHERE is_select AND statement_hash IS NOT NULL
  GROUP BY warehouse_id, statement_hash
),
repeated_agg AS (
  SELECT warehouse_id, SUM(CASE WHEN n > 1 THEN n ELSE 0 END) AS repeated_query_rows,
         SUM(n) AS texted_selects
  FROM stmt_counts
  GROUP BY warehouse_id
),
agg AS (
  SELECT warehouse_id,
         COUNT(*) AS queries,
         SUM(CASE WHEN is_select THEN 1 ELSE 0 END) AS select_queries,
         SUM(CASE WHEN is_select AND from_result_cache THEN 1 ELSE 0 END) AS from_cache_queries,
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
  a.select_queries,
  a.queries - a.select_queries AS other_statements,
  CASE WHEN a.select_queries > 0 THEN ROUND(a.from_cache_queries * 100.0 / a.select_queries, 1) END AS from_result_cache_pct,
  ROUND(a.avg_read_io_cache_percent, 1) AS avg_read_io_cache_percent,
  CASE WHEN r.texted_selects > 0 THEN ROUND(r.repeated_query_rows * 100.0 / r.texted_selects, 1) END AS repeated_statement_pct,
  ROUND(a.median_gap_seconds / 60.0, 1) AS median_gap_minutes,
  CASE
    WHEN a.select_queries < 10 THEN 'OK'
    -- Warn only when auto-stop sits ABOVE this warehouse's own type minimum -- 17 of 19 WARN
    -- warehouses on the fixture were already at the pro/classic floor (10 min) with nothing left to
    -- lower, so a fixed :warn_long_autostop_minutes threshold flagged them for a fix that does not
    -- exist. A type this query does not recognise (warehouse_kind NULL) never warns on auto-stop
    -- alone, rather than guess a floor for it.
    WHEN (a.from_cache_queries * 100.0 / a.select_queries) < 5
         -- auto-stop off (0) leaves the most room to lower
         AND CASE WHEN w.auto_stop_minutes = 0 THEN TRUE
                  WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN w.auto_stop_minutes > 5
                  WHEN upper(w.warehouse_type) IN ('PRO', 'CLASSIC') THEN w.auto_stop_minutes > 10
                  ELSE FALSE END
      THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM agg a
LEFT JOIN wh w ON w.warehouse_id = a.warehouse_id
LEFT JOIN repeated_agg r ON r.warehouse_id = a.warehouse_id
WHERE a.queries > 0
ORDER BY CASE status WHEN 'WARN' THEN 0 ELSE 1 END, from_result_cache_pct ASC NULLS LAST
) q
