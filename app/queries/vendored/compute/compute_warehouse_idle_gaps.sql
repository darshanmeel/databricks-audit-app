-- query_id: compute_warehouse_idle_gaps
-- title: SQL warehouse idle tail before auto-stop
-- domain: compute   tier: standard
-- reads: system.compute.warehouse_events, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.compute, system.billing; GA
-- params: :period_days (default 30) rolling window in days; :warn_idle_hours (default 4) hours of a single continuous RUNNING stretch that flags WARN; :crit_idle_hours (default 24) hours that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: The event-type enum, event columns and the cost-rollup join were verified against system.compute.warehouse_events and system.billing.usage/list_prices in a live workspace. The event side now uses compute_warehouse_idle_minutes' own state-segment logic (bounds, carry-in, state_rank tie-break, the last segment closed at the window end) in place of a plain per-event LEAD, which has not itself run on a live workspace yet. Confirm on your own account: 1) running_seconds for a warehouse you watched matches the time its monitoring page shows it running; 2) a warehouse still RUNNING with no STOPPED event yet shows running time up to now, not a NULL/zero gap.
-- read_this: One row = one SQL warehouse's time spent RUNNING, STARTING and STOPPED over the window. The columns that matter are running_seconds (total contiguous RUNNING time - RUNNING, SCALED_UP, or SCALED_DOWN with clusters left running - a mix of active query time and the idle tail before auto-stop, since per-query activity is deliberately not joined into this query) and max_running_gap_seconds (the single longest contiguous RUNNING period - an unusually long one usually means the warehouse never triggered its own auto-stop for a long time).
-- healthy: status = OK (max_running_gap_seconds below :warn_idle_hours hours) - field heuristic; tune :warn_idle_hours / :crit_idle_hours to your typical auto_stop_minutes settings.
-- investigate_if: status = WARN or CRITICAL (a single RUNNING stretch at or above :warn_idle_hours / :crit_idle_hours) - field heuristic. status = NOT_ASSESSED means this warehouse had zero RUNNING-type events in the window (running_seconds=0, no contiguous running period to measure), which this query still surfaces rather than dropping, since an unused warehouse is itself worth a look.
-- actions: 1) lower auto_stop_minutes on the warehouse in sql_warehouse_config_current so it suspends sooner after the last query (free); 2) route the warehouse's workload onto a shared/Serverless warehouse if it is mostly idle between bursts (config); 3) downsize warehouse_size if the long RUNNING stretch reflects genuinely low, steady load rather than a stuck auto-stop (spend).
-- next: sql_warehouse_config_current (to check this warehouse's auto_stop_minutes), sql_warehouse_events_activity (for the raw event-type breakdown), compute_warehouse_autoscale_churn (if the same warehouse is also thrashing clusters up/down)
-- caveats: Authoritative 6-value event_type enum for system.compute.warehouse_events is SCALED_UP, SCALED_DOWN, STOPPING, RUNNING, STARTING, STOPPED (SCALING_UP/SCALING_DOWN are undocumented and ignored). RUNNING TIME is computed with compute_warehouse_idle_minutes' own state-segment logic: the window is [today - :period_days, today) in whole UTC days, matching the cost rollup below rather than an uncapped `current_timestamp() - INTERVAL :period_days DAYS`; the state at the window's start is carried in from the last event before it; an event tie at the same timestamp is broken by state_rank (STARTING, RUNNING, SCALED_*, STOPPING, STOPPED, the last one holds); "running" is RUNNING, SCALED_UP, or SCALED_DOWN with cluster_count > 0 (a SCALED_DOWN to 0 clusters is a stop); each event's state holds until the next event, or the window's end for the last one - a warehouse still RUNNING at the window's end is counted up to the window's end, never left as an open/NULL gap. max_running_gap_seconds is the longest CONTIGUOUS running period - consecutive running-type events (RUNNING, then a SCALED_UP/DOWN, then RUNNING again, with no down-state gap between them) merge into one period, not the single longest per-event LEAD gap. This query still returns a row for a warehouse with warehouse_events activity but zero RUNNING-type events (running_seconds=0), rather than dropping it - the LEFT JOIN to the cost rollup never filters out a warehouse_id, so idle/unused warehouses stay visible here too. Per-event query activity is NOT joined into this query on purpose (that join fans out at day grain and overstates query counts) - keep this query event-only, and read running_seconds as "time RUNNING", not "time definitely idle". system.compute.warehouse_events carries no DBU/$ by itself; net_dbus/est_usd_list come from the separate cost rollup below, over the SAME [today - :period_days, today) span as the event side. net_dbus is exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1) - NOT your negotiated invoice rate (not available in any system table) and excludes cloud infra/egress cost; treat est_usd_list as directional. Cost is attributed by warehouse_id (billing ID) over the :period_days window (per-warehouse), not per event/idle-gap - the cost rollup is pre-aggregated before the join, so rows here are never multiplied by it. warehouse_id is a globally-unique GUID, so the rollup is keyed on warehouse_id alone (workspace_id dropped) to match this query's warehouse-only grain. price_basis is 'unpriced' when any non-free-usage SKU billed to this warehouse had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH bounds AS (
    SELECT unix_timestamp(CAST(current_date() - INTERVAL :period_days DAYS AS TIMESTAMP)) AS win_start_s,
           unix_timestamp(CAST(current_date() AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
    SELECT e.warehouse_id, e.event_type, e.cluster_count,
           unix_timestamp(e.event_time) AS t,
           CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                             WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                             WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
    FROM system.compute.warehouse_events e
    WHERE e.event_time < current_date()
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
    FROM system.billing.list_prices
),
cost_rollup AS (
    -- Pre-aggregated per-warehouse DBU/$ over the SAME [today - :period_days, today) window the
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
    -- status: worst-first band on the single longest continuous RUNNING period (field heuristic; :warn_idle_hours / :crit_idle_hours).
    CASE
        WHEN m.max_running_gap_seconds IS NULL THEN 'NOT_ASSESSED'
        WHEN m.max_running_gap_seconds >= :crit_idle_hours * 3600 THEN 'CRITICAL'
        WHEN m.max_running_gap_seconds >= :warn_idle_hours * 3600 THEN 'WARN'
        ELSE 'OK'
    END AS status
FROM wh_time t
LEFT JOIN max_run m ON m.warehouse_id = t.warehouse_id
LEFT JOIN cost_rollup cr ON cr.warehouse_id = t.warehouse_id
-- a dormant or deleted warehouse otherwise still gets a row here from the carry-in event alone
-- (see prior above, which scans all of warehouse_events history with no lower bound) -- keep only
-- one that ran in the window, or had at least one real event inside it.
WHERE COALESCE(t.running_seconds, 0) > 0 OR t.real_event_count > 0
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END, max_running_gap_seconds DESC
