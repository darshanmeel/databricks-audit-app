-- query_id: compute_warehouse_autoscale_churn
-- title: SQL warehouse autoscale churn (scale-up/scale-down thrash)
-- domain: compute   tier: standard
-- reads: system.compute.warehouse_events, system.compute.warehouses, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.compute, system.billing; GA
-- params: :period_days (default 30) rolling window in days; :min_observed_hours (default 1) minimum hours between a warehouse's first and last event in the window before a churn rate is reported (guards divide-by-zero and short-span spikes); :warn_churn_per_hour (default 4) true churn events (nonzero-to-nonzero cluster-count transitions) per observed hour that flags WARN; :crit_churn_per_hour (default 10) the same that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: Event-type enum and the cost-rollup join verified against system.compute.warehouse_events and system.billing.usage/list_prices in a live workspace. The churn-vs-start/stop split (LAG'd prev_cluster_count over ALL event types) is a straightforward reading of the documented enum, but has not itself been confirmed against a live account's actual event ordering.
-- read_this: One row = one SQL warehouse's scale-up/scale-down activity over the window. churn_events is TRUE autoscale churn - a SCALED_UP or SCALED_DOWN transition between two already-nonzero cluster counts - and is what status now bands on, per observed hour (observed_hours: the span between this warehouse's first and last event in the window). start_events/stop_events count the warehouse's own 0->N starts and N->0 stops separately (not churn); start_stop_cycles is the number of full start-then-stop cycles observed. scaling_events/scaled_up_events/scaled_down_events/scaling_events_total are KEPT with their original meaning (every SCALED_UP/SCALED_DOWN event, starts and stops included) for compatibility with anything already reading them.
-- healthy: status = OK - field heuristic; tune :warn_churn_per_hour / :crit_churn_per_hour for your account.
-- investigate_if: status = WARN or CRITICAL (churn_events / observed_hours at or above the threshold) - field heuristic. status = NOT_ASSESSED means observed_hours is below :min_observed_hours, i.e. too little history to rate, not a clean bill of health.
-- actions: 1) widen the warehouse's min_clusters/max_clusters autoscale bounds, or switch it to a fixed cluster count, so it stops thrashing (free); 2) raise auto_stop_minutes or route bursty/spiky workloads to a dedicated warehouse so scaling settles (config); 3) move the worst-churning warehouse to Serverless SQL, which absorbs scaling internally (spend).
-- next: sql_warehouse_config_current (to see this warehouse's min_clusters/max_clusters), compute_warehouse_idle_gaps (if the same warehouse also shows a long RUNNING idle tail)
-- caveats: Authoritative event_type enum for system.compute.warehouse_events is SCALED_UP, SCALED_DOWN, STOPPING, RUNNING, STARTING, STOPPED. Only SCALED_UP/SCALED_DOWN count toward any of the event columns here. churn_events reads each event's own previous cluster_count (LAG over ALL event types in the window, ordered by event_time) and counts a SCALED_UP/SCALED_DOWN transition as churn only when BOTH the prior and the new cluster_count are > 0 - a transition FROM 0 is a start_event, a transition TO 0 is a stop_event, neither is churn. The very first in-window event has no earlier row to look back to and is treated as prev_cluster_count = 0 (the conservative reading at the window's edge, so it can only ever read as a start, never churn). avg_cluster_count_time_weighted weights each observed cluster_count by how long the warehouse actually held it before the next event (LEAD(event_time)); the last event in the window contributes no weighted duration, since it has no next event yet. The churn rate is churn_events / observed_hours; observed_hours is the span between this warehouse's first and last event IN THE WINDOW, not the wall-clock window, so a warehouse seen for only part of the window is not diluted. A warehouse with a single event has a zero/near-zero observed span, so status is NOT_ASSESSED below :min_observed_hours rather than a fabricated spike. cluster_count is clusters running at event time. system.compute.warehouse_events carries no DBU/$ by itself - it is a behavioral churn signal only; net_dbus/est_usd_list come from the separate cost rollup below. net_dbus is exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1) - NOT your negotiated invoice rate (not available in any system table) and excludes cloud infra/egress cost; treat est_usd_list as directional. Cost is attributed by billing warehouse_id over the :period_days window (per-resource), not per scaling event - the cost rollup is pre-aggregated before the join, so rows here are never multiplied by it. warehouse_id is a globally-unique GUID, so the cost rollup is keyed on warehouse_id alone (workspace_id is not part of the rollup grain). warehouse_name comes from the latest non-deleted system.compute.warehouses row for the id and may be null if the warehouse has since been deleted. price_basis is 'unpriced' when any non-free-usage SKU billed to this warehouse had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH price AS (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
    FROM system.billing.list_prices
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
    FROM system.billing.usage u
    LEFT JOIN price p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
     AND u.usage_end_time >= p.price_start_time
     AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
    WHERE upper(u.usage_unit) = 'DBU'
      AND u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_date >= current_date() - INTERVAL :period_days DAYS
      AND u.usage_date <  current_date()
    GROUP BY u.usage_metadata.warehouse_id
),
latest_wh AS (
  SELECT warehouse_id, warehouse_name,
         ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
  FROM system.compute.warehouses
),
wh_name AS (SELECT warehouse_id, warehouse_name FROM latest_wh WHERE rn = 1),
ev AS (
  -- prev_cluster_count from ALL event types (not just SCALED_UP/DOWN), so a 0->N transition
  -- logged as SCALED_UP is correctly told apart from a churn transition between nonzero counts.
  -- next_event_time (LEAD) gives each event's own held duration, for the time-weighted average.
  SELECT warehouse_id, event_type, cluster_count, event_time,
         LAG(cluster_count) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS prev_cluster_count,
         LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time)   AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= current_timestamp() - INTERVAL :period_days DAYS
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
        WHEN (unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0 < :min_observed_hours THEN 'NOT_ASSESSED'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / ((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0) >= :crit_churn_per_hour THEN 'CRITICAL'
        WHEN SUM(CASE WHEN e.event_type IN ('SCALED_UP', 'SCALED_DOWN')
                       AND COALESCE(e.prev_cluster_count, 0) > 0 AND COALESCE(e.cluster_count, 0) > 0
                      THEN 1 ELSE 0 END)
             / ((unix_timestamp(MAX(e.event_time)) - unix_timestamp(MIN(e.event_time))) / 3600.0) >= :warn_churn_per_hour THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM ev e
LEFT JOIN cost_rollup cr ON e.warehouse_id = cr.warehouse_id
LEFT JOIN wh_name wn ON wn.warehouse_id = e.warehouse_id
GROUP BY e.warehouse_id, wn.warehouse_name, cr.net_dbus, cr.est_usd_list, cr.price_basis
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, churn_events DESC
