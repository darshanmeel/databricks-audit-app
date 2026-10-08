-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_idle_minutes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_idle_minutes.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH bounds AS (
  -- the window as epoch seconds: [first day 00:00, today 00:00)
  SELECT unix_timestamp(CAST(__AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AS TIMESTAMP)) AS win_start_s,
         unix_timestamp(CAST(__AS_OF_DATE__ AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
  -- every documented event before today; state_rank orders events that share a timestamp
  SELECT e.workspace_id, e.warehouse_id, e.event_type, e.cluster_count,
         unix_timestamp(e.event_time) AS t,
         CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                           WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                           WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
  FROM `system`.`compute`.`warehouse_events` e
  WHERE e.event_time < __AS_OF_DATE__
    AND e.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
prior AS (
  -- the last event before the window: the state the warehouse was in when the window opened
  SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count
  FROM ev_all a CROSS JOIN bounds b
  WHERE a.t < b.win_start_s
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.warehouse_id
                             ORDER BY a.t DESC, a.state_rank DESC) = 1
),
ev AS (
  SELECT x.*,
         CASE WHEN x.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_up,
         CASE WHEN x.event_type IN ('RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_running,
         GREATEST(COALESCE(x.cluster_count, 1), 1) AS clusters
  FROM (
    SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank,
           0 AS carried
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t >= b.win_start_s
    UNION ALL
    SELECT p.workspace_id, p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t,
           0 AS state_rank, 1 AS carried
    FROM prior p CROSS JOIN bounds b
  ) x
),
seg AS (
  -- one row per event: the state it holds until the next event (or the window's end)
  SELECT v.*,
         LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank) AS next_t,
         COALESCE(LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank),
                  b.win_end_s) AS seg_end_s,
         COALESCE(LAG(v.is_running) OVER (PARTITION BY v.workspace_id, v.warehouse_id
                                          ORDER BY v.t, v.state_rank), 0) AS prev_running,
         ROW_NUMBER() OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank) AS seq
  FROM ev v CROSS JOIN bounds b
),
seg_runs AS (
  SELECT s.*,
         SUM(CASE WHEN s.is_running = 1 AND s.prev_running = 0 THEN 1 ELSE 0 END)
           OVER (PARTITION BY s.workspace_id, s.warehouse_id ORDER BY s.t, s.state_rank
                 ROWS UNBOUNDED PRECEDING) AS run_no
  FROM seg s
),
running AS (
  -- contiguous running periods
  SELECT workspace_id, warehouse_id, run_no,
         MIN(t) AS run_start_s, MAX(seg_end_s) AS run_end_s
  FROM seg_runs
  WHERE is_running = 1
  GROUP BY workspace_id, warehouse_id, run_no
),
wh_time AS (
  SELECT workspace_id, warehouse_id,
         MIN(t)                                                        AS measured_from_s,
         MAX(CASE WHEN seq = 1 AND (carried = 1 OR event_type = 'STARTING') THEN 1 ELSE 0 END) AS start_state_known,
         SUM(is_up * (seg_end_s - t))                                  AS up_s,
         SUM(is_running * (seg_end_s - t))                             AS running_s,
         SUM(is_up * (seg_end_s - t) * clusters)                       AS up_cluster_s,
         MAX(CASE WHEN is_up = 1 THEN clusters END)                    AS max_clusters_seen,
         MAX(CASE WHEN is_running = 1 AND next_t IS NULL THEN 1 ELSE 0 END) AS open_at_window_end
  FROM seg
  GROUP BY workspace_id, warehouse_id
),
running_periods AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS running_periods FROM running
  GROUP BY workspace_id, warehouse_id
),
qh AS (
  -- any statement history at all in the window? (none = the source is missing, not "all idle")
  SELECT COUNT(*) AS qh_rows
  FROM `system`.`query`.`history`
  WHERE start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND start_time < __AS_OF_DATE__
),
stmt AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id,
         GREATEST(unix_timestamp(q.start_time), b.win_start_s) AS s,
         LEAST(COALESCE(unix_timestamp(q.end_time), unix_timestamp(q.update_time),
                        unix_timestamp(q.start_time)), b.win_end_s) AS e
  FROM `system`.`query`.`history` q CROSS JOIN bounds b
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS - INTERVAL 48 HOURS
    AND q.start_time < __AS_OF_DATE__
),
stmt_ord AS (
  SELECT workspace_id, warehouse_id, s, e,
         MAX(e) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY s, e
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_e
  FROM stmt
  WHERE e >= s
),
stmt_isl AS (
  SELECT workspace_id, warehouse_id, s, e,
         SUM(CASE WHEN prev_max_e IS NULL OR s > prev_max_e THEN 1 ELSE 0 END)
           OVER (PARTITION BY workspace_id, warehouse_id ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS island_no
  FROM stmt_ord
),
busy AS (
  SELECT workspace_id, warehouse_id, island_no, MIN(s) AS bs, MAX(e) AS be
  FROM stmt_isl
  GROUP BY workspace_id, warehouse_id, island_no
),
pb AS (
  -- busy islands clipped to each running period they touch
  SELECT r.workspace_id, r.warehouse_id, r.run_no,
         GREATEST(x.bs, r.run_start_s) AS bs, LEAST(x.be, r.run_end_s) AS be
  FROM running r
  JOIN busy x
    ON  x.workspace_id = r.workspace_id AND x.warehouse_id = r.warehouse_id
    AND x.bs <= r.run_end_s AND x.be >= r.run_start_s
),
qgap AS (
  -- time from one busy stretch's end to the next one's start: what an auto-stop setting acts on;
  -- the last stretch's gap runs to the end of its running period, so the final tail is replayed too
  SELECT p.workspace_id, p.warehouse_id,
         COALESCE(LEAD(p.bs) OVER (PARTITION BY p.workspace_id, p.warehouse_id ORDER BY p.bs), r.run_end_s) - p.be AS g,
         -- the final tail has no next query, so stopping after it costs no cold start
         LEAD(p.bs) OVER (PARTITION BY p.workspace_id, p.warehouse_id ORDER BY p.bs) IS NULL AS is_tail
  FROM pb p
  JOIN running r
    ON r.workspace_id = p.workspace_id AND r.warehouse_id = p.warehouse_id AND r.run_no = p.run_no
),
busy_agg AS (
  SELECT workspace_id, warehouse_id, SUM(be - bs) AS busy_s FROM pb GROUP BY workspace_id, warehouse_id
),
marks AS (
  SELECT workspace_id, warehouse_id, run_no, run_start_s AS bs, run_start_s AS be, 0 AS kind FROM running
  UNION ALL
  SELECT workspace_id, warehouse_id, run_no, bs, be, 1 AS kind FROM pb
  UNION ALL
  SELECT workspace_id, warehouse_id, run_no, run_end_s AS bs, run_end_s AS be, 2 AS kind FROM running
),
gaps AS (
  SELECT workspace_id, warehouse_id, run_no, kind AS opened_by, be AS gap_start_s,
         LEAD(bs)   OVER (PARTITION BY workspace_id, warehouse_id, run_no ORDER BY bs, kind, be) AS gap_end_s,
         LEAD(kind) OVER (PARTITION BY workspace_id, warehouse_id, run_no ORDER BY bs, kind, be) AS closed_by
  FROM marks
),
idle AS (
  SELECT g.workspace_id, g.warehouse_id, g.gap_start_s, g.gap_end_s,
         g.gap_end_s - g.gap_start_s AS gap_s,
         CASE WHEN g.opened_by = 0 AND g.closed_by = 2 THEN 'no_query'
              WHEN g.opened_by = 0 THEN 'start'
              WHEN g.closed_by = 2 THEN 'tail'
              ELSE 'between' END AS gap_kind
  FROM gaps g
  WHERE g.gap_end_s IS NOT NULL AND g.gap_end_s > g.gap_start_s
),
idle_agg AS (
  SELECT workspace_id, warehouse_id,
         SUM(gap_s) AS idle_s,
         SUM(CASE WHEN gap_s > 60 THEN gap_s ELSE 0 END) AS counted_s,
         SUM(CASE WHEN gap_s > 60 THEN 1 ELSE 0 END)     AS counted_gaps,
         MAX(CASE WHEN gap_s > 60 THEN gap_s END)        AS longest_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'start'    THEN gap_s ELSE 0 END) AS start_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'between'  THEN gap_s ELSE 0 END) AS between_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'tail'     THEN gap_s ELSE 0 END) AS tail_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'no_query' THEN gap_s ELSE 0 END) AS no_query_s,
         SUM(CASE WHEN gap_s > 60 AND gap_kind = 'no_query' THEN 1 ELSE 0 END)     AS no_query_periods
  FROM idle
  GROUP BY workspace_id, warehouse_id
),
counted_cw AS (
  -- cluster-seconds inside each counted gap, from the running segments it spans
  SELECT i.workspace_id, i.warehouse_id,
         SUM((LEAST(i.gap_end_s, s.seg_end_s) - GREATEST(i.gap_start_s, s.t)) * s.clusters) AS idle_cluster_s
  FROM idle i
  JOIN seg s
    ON  s.workspace_id = i.workspace_id AND s.warehouse_id = i.warehouse_id AND s.is_running = 1
    AND s.t < i.gap_end_s AND s.seg_end_s > i.gap_start_s
  WHERE i.gap_s > 60
  GROUP BY i.workspace_id, i.warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
bill_rows AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id,
         unix_timestamp(u.usage_start_time) AS us,
         u.usage_quantity AS dbus,
         u.usage_quantity * COALESCE(p.list_rate, 0) AS usd,
         CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_q,
         CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_q,
         CASE WHEN u.product_features.is_serverless = TRUE THEN 1 ELSE 0 END AS serverless
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND u.usage_date < __AS_OF_DATE__
),
bill AS (
  SELECT r.workspace_id, r.warehouse_id,
         SUM(r.dbus) AS net_dbus,
         SUM(r.usd)  AS usd_all,
         SUM(CASE WHEN t.measured_from_s IS NOT NULL
                   AND r.us >= FLOOR(t.measured_from_s / 3600) * 3600 THEN r.usd END) AS measured_usd,
         CASE WHEN SUM(r.unpriced_q) > 0 THEN 'unpriced'
              WHEN SUM(r.priced_q) = 0   THEN 'free'
              ELSE 'priced' END AS price_basis,
         MAX(r.serverless) AS any_serverless
  FROM bill_rows r
  LEFT JOIN wh_time t ON t.workspace_id = r.workspace_id AND t.warehouse_id = r.warehouse_id
  GROUP BY r.workspace_id, r.warehouse_id
),
cfg AS (
  SELECT warehouse_id, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
whatif AS (
  -- replay the gaps under an auto-stop of N minutes: a gap up to N stays idle, a longer one idles
  -- N minutes, stops, and the next query waits for a cold start
  SELECT q.workspace_id, q.warehouse_id,
         SUM(LEAST(q.g, 60))   AS idle_1_s,  SUM(CASE WHEN q.g > 60   AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_1,
         SUM(LEAST(q.g, 120))  AS idle_2_s,  SUM(CASE WHEN q.g > 120  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_2,
         SUM(LEAST(q.g, 300))  AS idle_5_s,  SUM(CASE WHEN q.g > 300  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_5,
         SUM(LEAST(q.g, 600))  AS idle_10_s, SUM(CASE WHEN q.g > 600  AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_10,
         SUM(LEAST(q.g, 1800)) AS idle_30_s, SUM(CASE WHEN q.g > 1800 AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_30,
         -- auto-stop 0 means it never stops: every gap stays idle
         SUM(CASE WHEN c.auto_stop_minutes > 0 THEN LEAST(q.g, c.auto_stop_minutes * 60) ELSE q.g END) AS idle_now_s,
         SUM(CASE WHEN c.auto_stop_minutes > 0 AND q.g > c.auto_stop_minutes * 60 AND NOT q.is_tail THEN 1 ELSE 0 END) AS starts_now
  FROM qgap q
  LEFT JOIN cfg c ON c.warehouse_id = q.warehouse_id
  WHERE q.g > 0
  GROUP BY q.workspace_id, q.warehouse_id
),
keys AS (
  SELECT workspace_id, warehouse_id FROM wh_time
  UNION
  SELECT workspace_id, warehouse_id FROM bill
),
joined AS (
  SELECT k.workspace_id, k.warehouse_id,
         c.warehouse_type, c.auto_stop_minutes,
         t.warehouse_id AS has_events, t.start_state_known, t.open_at_window_end,
         t.up_s, t.running_s, t.up_cluster_s, t.max_clusters_seen,
         rp.running_periods,
         ba.busy_s,
         ia.idle_s, ia.counted_s, ia.counted_gaps, ia.longest_s, ia.start_s, ia.between_s,
         ia.tail_s, ia.no_query_s, ia.no_query_periods,
         cw.idle_cluster_s,
         bl.net_dbus, bl.usd_all, bl.measured_usd, bl.price_basis, bl.any_serverless,
         wi.idle_1_s, wi.starts_1, wi.idle_2_s, wi.starts_2, wi.idle_5_s, wi.starts_5, wi.idle_10_s, wi.starts_10,
         wi.idle_30_s, wi.starts_30, wi.idle_now_s, wi.starts_now,
         q.qh_rows,
         CASE WHEN t.warehouse_id IS NULL THEN 'no_warehouse_events'
              WHEN q.qh_rows = 0          THEN 'no_query_history'
              WHEN t.running_s = 0        THEN 'no_running_time_measured'
         END AS na_reason
  FROM keys k
  CROSS JOIN qh q
  LEFT JOIN wh_time t          ON t.workspace_id = k.workspace_id  AND t.warehouse_id = k.warehouse_id
  LEFT JOIN running_periods rp ON rp.workspace_id = k.workspace_id AND rp.warehouse_id = k.warehouse_id
  LEFT JOIN busy_agg ba        ON ba.workspace_id = k.workspace_id AND ba.warehouse_id = k.warehouse_id
  LEFT JOIN idle_agg ia        ON ia.workspace_id = k.workspace_id AND ia.warehouse_id = k.warehouse_id
  LEFT JOIN counted_cw cw      ON cw.workspace_id = k.workspace_id AND cw.warehouse_id = k.warehouse_id
  LEFT JOIN bill bl            ON bl.workspace_id = k.workspace_id AND bl.warehouse_id = k.warehouse_id
  LEFT JOIN cfg c              ON c.warehouse_id  = k.warehouse_id
  LEFT JOIN whatif wi          ON wi.workspace_id = k.workspace_id AND wi.warehouse_id = k.warehouse_id
  WHERE COALESCE(t.running_s, 0) > 0 OR bl.net_dbus IS NOT NULL
),
priced AS (
  SELECT j.*,
         CASE WHEN j.na_reason IS NULL AND j.price_basis IN ('priced', 'free')
               AND j.measured_usd IS NOT NULL AND j.up_cluster_s > 0
              THEN j.measured_usd * COALESCE(j.idle_cluster_s, 0) / j.up_cluster_s END AS wasted_raw,
         CASE WHEN j.na_reason IS NULL AND j.price_basis IN ('priced', 'free')
               AND j.measured_usd IS NOT NULL AND j.up_cluster_s > 0
              THEN j.measured_usd * 60 / j.up_cluster_s END AS usd_per_cluster_minute
  FROM joined j
)
SELECT p.workspace_id,
       p.warehouse_id,
       CASE WHEN upper(p.warehouse_type) = 'SERVERLESS' OR p.any_serverless = 1 THEN 'serverless'
            WHEN upper(p.warehouse_type) = 'PRO'     THEN 'pro'
            WHEN upper(p.warehouse_type) = 'CLASSIC' THEN 'classic'
       END                                                               AS warehouse_kind,
       p.auto_stop_minutes,
       CASE WHEN p.has_events IS NULL THEN NULL ELSE p.start_state_known = 1 END   AS start_state_known,
       CASE WHEN p.has_events IS NULL THEN NULL ELSE p.open_at_window_end = 1 END  AS open_at_window_end,
       COALESCE(p.running_periods, 0)                                    AS running_periods,
       ROUND(p.up_s / 60.0, 1)                                           AS up_minutes,
       ROUND(p.running_s / 60.0, 1)                                      AS running_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.busy_s, 0) / 60.0, 1) END              AS busy_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.idle_s, 0) / 60.0, 1) END              AS idle_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.counted_s, 0) / 60.0, 1) END           AS counted_idle_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE COALESCE(p.counted_gaps, 0) END                         AS counted_idle_gaps,
       -- the same NULL rule for these five
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.longest_s, 0) / 60.0, 1) END           AS longest_idle_gap_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.start_s, 0) / 60.0, 1) END             AS start_gap_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.between_s, 0) / 60.0, 1) END           AS between_queries_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.tail_s, 0) / 60.0, 1) END              AS stop_tail_minutes,
       CASE WHEN p.na_reason IN ('no_warehouse_events', 'no_query_history') THEN NULL
            ELSE ROUND(COALESCE(p.no_query_s, 0) / 60.0, 1) END          AS no_query_minutes,
       CASE WHEN p.na_reason IS NULL
            THEN ROUND(COALESCE(p.counted_s, 0) * 100.0 / p.running_s, 1) END AS idle_share_pct,
       ROUND(p.up_cluster_s / 60.0, 1)                                   AS up_cluster_minutes,
       CASE WHEN p.na_reason IS NULL
            THEN ROUND(COALESCE(p.idle_cluster_s, 0) / 60.0, 1) END      AS idle_cluster_minutes,
       p.max_clusters_seen,
       ROUND(p.net_dbus, 2)                                              AS net_dbus,
       CASE WHEN p.price_basis IN ('priced', 'free') THEN ROUND(p.usd_all, 2) END AS est_usd_list,
       ROUND(p.wasted_raw, 2)                                            AS est_wasted_usd_list,
       p.price_basis,
       60                                                 AS idle_gap_seconds,
       -- no statement history: no gaps to replay; one busy stretch: no gaps, so 0
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_1_s, 0) / 60.0, 1) END  AS autostop_1_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_1, 0) END                   AS autostop_1_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_2_s, 0) / 60.0, 1) END  AS autostop_2_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_2, 0) END                   AS autostop_2_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_5_s, 0) / 60.0, 1) END  AS autostop_5_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_5, 0) END                   AS autostop_5_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_10_s, 0) / 60.0, 1) END AS autostop_10_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_10, 0) END                  AS autostop_10_cold_starts,
       CASE WHEN p.qh_rows > 0 THEN ROUND(COALESCE(p.idle_30_s, 0) / 60.0, 1) END AS autostop_30_idle_minutes,
       CASE WHEN p.qh_rows > 0 THEN COALESCE(p.starts_30, 0) END                  AS autostop_30_cold_starts,
       CASE WHEN p.qh_rows > 0 AND p.auto_stop_minutes IS NOT NULL
            THEN ROUND(COALESCE(p.idle_now_s, 0) / 60.0, 1) END                    AS autostop_now_idle_minutes,
       CASE WHEN p.qh_rows > 0 AND p.auto_stop_minutes IS NOT NULL
            THEN COALESCE(p.starts_now, 0) END                                     AS autostop_now_cold_starts,
       ROUND(p.usd_per_cluster_minute, 4)                                        AS usd_per_cluster_minute,
       CASE
         WHEN p.na_reason = 'no_warehouse_events' THEN
           'billed in the window but no start or stop events were recorded for it, so its running time cannot be measured; if auto-stop is off it may have run the whole window'
         WHEN p.na_reason = 'no_query_history' THEN
           'no SQL statement history was exported for the window, so time running a query cannot be told apart from idle time'
         WHEN p.na_reason = 'no_running_time_measured' THEN
           'billed in the window, but its events show no running time that can be measured (it was already running when its first recorded event in the window arrived)'
         ELSE
           CONCAT_WS('; ',
             CONCAT('idle ', CAST(ROUND(COALESCE(p.counted_s, 0) / 60.0, 0) AS BIGINT), ' of ',
                    CAST(ROUND(p.running_s / 60.0, 0) AS BIGINT), ' running min (',
                    CAST(ROUND(COALESCE(p.counted_s, 0) * 100.0 / p.running_s, 0) AS BIGINT), '%) in ',
                    COALESCE(p.counted_gaps, 0),
                    CASE WHEN COALESCE(p.counted_gaps, 0) = 1 THEN ' gap' ELSE ' gaps' END,
                    ' longer than ', 60, ' s'),
             CASE WHEN COALESCE(p.counted_gaps, 0) > 0
                  THEN CONCAT('longest ', CAST(ROUND(COALESCE(p.longest_s, 0) / 60.0, 0) AS BIGINT), ' min') END,
             CASE WHEN COALESCE(p.tail_s, 0) > 0
                  THEN CONCAT(CAST(ROUND(COALESCE(p.tail_s, 0) / 60.0, 0) AS BIGINT),
                              ' min after the last query, waiting for auto-stop') END,
             CASE WHEN COALESCE(p.no_query_s, 0) > 0
                  THEN CONCAT(CAST(ROUND(COALESCE(p.no_query_s, 0) / 60.0, 0) AS BIGINT), ' min in ',
                              COALESCE(p.no_query_periods, 0),
                              CASE WHEN COALESCE(p.no_query_periods, 0) = 1 THEN ' running period' ELSE ' running periods' END,
                              ' with no query at all') END,
             CASE WHEN p.wasted_raw IS NULL
                  THEN CONCAT('no dollar figure: ',
                              CASE WHEN p.price_basis = 'unpriced' THEN 'its usage has no list price'
                                   ELSE 'no billing rows for it in the window' END) END)
       END                                                                AS waste_reason,
       p.na_reason                                                       AS not_assessed_reason,
       CASE
         WHEN p.na_reason IS NOT NULL THEN 'NOT_ASSESSED'
         -- a NULL (unpriced) wasted_raw never meets a dollar band
         WHEN (COALESCE(p.counted_s, 0) >= 30 * 60
               AND COALESCE(p.counted_s, 0) * 100.0 / p.running_s >= 60)
              OR p.wasted_raw >= 200 THEN 'CRITICAL'
         WHEN (COALESCE(p.counted_s, 0) >= 30 * 60
               AND COALESCE(p.counted_s, 0) * 100.0 / p.running_s >= 30)
              OR p.wasted_raw >= 20 THEN 'WARN'
         ELSE 'OK'
       END                                                                AS status
FROM priced p
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_wasted_usd_list DESC NULLS LAST, counted_idle_minutes DESC NULLS LAST,
         workspace_id, warehouse_id
) q
