-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_idle_gaps.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/compute_warehouse_idle_gaps.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH bounds AS (
    SELECT unix_timestamp(CAST(__AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS AS TIMESTAMP)) AS win_start_s,
           unix_timestamp(CAST(__AS_OF_DATE__ AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
    SELECT e.warehouse_id, e.event_type, e.cluster_count,
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
    SELECT a.warehouse_id, a.event_type, a.cluster_count
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t < b.win_start_s
    QUALIFY ROW_NUMBER() OVER (PARTITION BY a.warehouse_id ORDER BY a.t DESC, a.state_rank DESC) = 1
),
ev AS (
    SELECT x.*,
           CASE WHEN x.event_type IN ('RUNNING', 'SCALED_UP')
                  OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
                THEN 1 ELSE 0 END AS is_running
    FROM (
        SELECT a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank, 1 AS is_real_event
        FROM ev_all a CROSS JOIN bounds b
        WHERE a.t >= b.win_start_s
        UNION ALL
        SELECT p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t, 0 AS state_rank, 0 AS is_real_event
        FROM prior p CROSS JOIN bounds b
    ) x
),
seg AS (
    -- one row per event: the state it holds until the next event (or the window's end)
    SELECT v.*,
           COALESCE(LEAD(v.t) OVER (PARTITION BY v.warehouse_id ORDER BY v.t, v.state_rank),
                    b.win_end_s) AS seg_end_s,
           COALESCE(LAG(v.is_running) OVER (PARTITION BY v.warehouse_id ORDER BY v.t, v.state_rank), 0) AS prev_running
    FROM ev v CROSS JOIN bounds b
),
seg_runs AS (
    SELECT s.*,
           SUM(CASE WHEN s.is_running = 1 AND s.prev_running = 0 THEN 1 ELSE 0 END)
             OVER (PARTITION BY s.warehouse_id ORDER BY s.t, s.state_rank ROWS UNBOUNDED PRECEDING) AS run_no
    FROM seg s
),
running AS (
    -- contiguous running periods: consecutive running-type events merge into one
    SELECT warehouse_id, run_no, MIN(t) AS run_start_s, MAX(seg_end_s) AS run_end_s
    FROM seg_runs
    WHERE is_running = 1
    GROUP BY warehouse_id, run_no
),
max_run AS (
    SELECT warehouse_id, MAX(run_end_s - run_start_s) AS max_running_gap_seconds
    FROM running
    GROUP BY warehouse_id
),
wh_time AS (
    SELECT warehouse_id,
           COUNT(*)                                                              AS event_count,
           SUM(is_real_event)                                                    AS real_event_count,
           SUM(is_running * (seg_end_s - t))                                     AS running_seconds,
           SUM(CASE WHEN event_type = 'STARTING' THEN seg_end_s - t ELSE 0 END)  AS starting_seconds,
           SUM(CASE WHEN event_type = 'STOPPED'  THEN seg_end_s - t ELSE 0 END)  AS stopped_seconds,
           SUM(CASE WHEN event_type = 'RUNNING'  THEN 1 ELSE 0 END)              AS running_events,
           SUM(CASE WHEN event_type = 'STARTING' THEN 1 ELSE 0 END)              AS starting_events,
           SUM(CASE WHEN event_type = 'STOPPED'  THEN 1 ELSE 0 END)              AS stopped_events
    FROM seg
    GROUP BY warehouse_id
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
cost_rollup AS (
    -- Pre-aggregated per-warehouse DBU/$ over the SAME [today - __WINDOW_DAYS__, today) window the
    -- event side above uses. warehouse_id is a globally-unique GUID, so we key on it alone.
    SELECT
        u.usage_metadata.warehouse_id                    AS warehouse_id,
        SUM(u.usage_quantity)                            AS net_dbus,
        SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
        CASE
          WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                        THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
          WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
          ELSE 'priced'
        END                                               AS price_basis
    FROM `system`.`billing`.`usage` u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
      AND u.usage_date <  __AS_OF_DATE__
    GROUP BY u.usage_metadata.warehouse_id
)
SELECT
    t.warehouse_id,
    t.event_count,
    COALESCE(t.running_seconds, 0)  AS running_seconds,
    COALESCE(t.starting_seconds, 0) AS starting_seconds,
    COALESCE(t.stopped_seconds, 0)  AS stopped_seconds,
    m.max_running_gap_seconds,
    COALESCE(t.running_events, 0)  AS running_events,
    COALESCE(t.starting_events, 0) AS starting_events,
    COALESCE(t.stopped_events, 0)  AS stopped_events,
    -- ADDED cost visibility (see header caveats): exact billed DBUs and list-price $ ESTIMATE for this warehouse
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- status: worst-first band on the single longest continuous RUNNING period (field heuristic; 4 / 24).
    CASE
        WHEN m.max_running_gap_seconds IS NULL THEN 'NOT_ASSESSED'
        WHEN m.max_running_gap_seconds >= 24 * 3600 THEN 'CRITICAL'
        WHEN m.max_running_gap_seconds >= 4 * 3600 THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM wh_time t
LEFT JOIN max_run m ON m.warehouse_id = t.warehouse_id
LEFT JOIN cost_rollup cr ON cr.warehouse_id = t.warehouse_id
WHERE COALESCE(t.running_seconds, 0) > 0 OR t.real_event_count > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, max_running_gap_seconds DESC
) q
