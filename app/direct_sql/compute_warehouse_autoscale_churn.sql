-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_autoscale_churn.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/compute_warehouse_autoscale_churn.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
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
    SELECT u.usage_metadata.warehouse_id                     AS warehouse_id,
           SUM(u.usage_quantity)                             AS net_dbus,
           SUM(u.usage_quantity * COALESCE(p.list_rate, 0))  AS est_usd_list,
           CASE
             WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                           THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
             WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
             ELSE 'priced'
           END                                                AS price_basis
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
),
latest_wh AS (
  SELECT warehouse_id, warehouse_name,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`warehouses`
),
wh_name AS (SELECT warehouse_id, warehouse_name FROM latest_wh WHERE rn = 1),
ev AS (
  -- prev_cluster_count from ALL event types (not just SCALED_UP/DOWN), so a 0->N transition
  -- logged as SCALED_UP is correctly told apart from a churn transition between nonzero counts.
  -- next_event_time (LEAD) gives each event's own held duration, for the time-weighted average.
  SELECT warehouse_id, event_type, cluster_count, event_time,
         LAG(cluster_count) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS prev_cluster_count,
         LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time)   AS next_event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= __AS_OF_TS__ - INTERVAL __WINDOW_DAYS__ DAYS
)
SELECT
    e.warehouse_id,
    wn.warehouse_name,
    -- original columns, meaning unchanged (front-end compatibility -- see read_this above)
    SUM(CASE WHEN e.event_type = 'SCALED_UP'   THEN 1 ELSE 0 END)                  AS scaled_up_events,
    SUM(CASE WHEN e.event_type = 'SCALED_DOWN' THEN 1 ELSE 0 END)                  AS scaled_down_events,
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END)  AS scaling_events,
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN') THEN 1 ELSE 0 END)  AS scaling_events_total,
    -- new columns: true churn (nonzero-to-nonzero transitions) apart from ordinary starts/stops
    SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
              AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
             THEN 1 ELSE 0 END)                                          AS churn_events,
    SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
              AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END)    AS start_events,
    SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
              AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END) AS stop_events,
    LEAST(
      SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
                AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END),
      SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
                AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END)
    )                                                                     AS start_stop_cycles,
    COUNT(*)                                                              AS total_events,
    MIN(e.event_time)                                                     AS first_event_time,
    MAX(e.event_time)                                                     AS last_event_time,
    (unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0 AS observed_hours,
    (SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
               THEN 1 ELSE 0 END)
     / NULLIF((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0, 0)) AS churn_events_per_hour,
    (LEAST(
       SUM(CASE WHEN e.event_type = 'SCALED_UP' AND COALESCE(e.prev_cluster_count, 0) = 0
                 AND COALESCE(e.cluster_count, 0) > 0 THEN 1 ELSE 0 END),
       SUM(CASE WHEN e.event_type = 'SCALED_DOWN' AND COALESCE(e.cluster_count, 0) = 0
                 AND COALESCE(e.prev_cluster_count, 0) > 0 THEN 1 ELSE 0 END)
     )
     / NULLIF((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0, 0)) AS start_stop_cycles_per_hour,
    MAX(e.cluster_count)                                                  AS max_cluster_count,
    AVG(e.cluster_count)                                                  AS avg_cluster_count,
    SUM(e.cluster_count * (unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time)))
      / NULLIF(SUM(unix_timestamp(e.next_event_time) - unix_timestamp(e.event_time)), 0) AS avg_cluster_count_time_weighted,
    COALESCE(cr.net_dbus, 0)     AS net_dbus,
    COALESCE(cr.est_usd_list, 0) AS est_usd_list,
    COALESCE(cr.price_basis, 'priced') AS price_basis,
    -- status: worst-first band on TRUE churn events (nonzero-to-nonzero) per observed hour - the
    -- fix - not the old scaling_events_total rate, which also counted ordinary starts and stops.
    CASE
        WHEN (unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0 < 1 THEN 'NOT_ASSESSED'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / ((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0) >= 10 THEN 'CRITICAL'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / ((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0) >= 4 THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM ev e
LEFT JOIN cost_rollup cr ON e.warehouse_id = cr.warehouse_id
LEFT JOIN wh_name wn ON wn.warehouse_id = e.warehouse_id
GROUP BY e.warehouse_id, wn.warehouse_name, cr.net_dbus, cr.est_usd_list, cr.price_basis
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, churn_events DESC
) q
