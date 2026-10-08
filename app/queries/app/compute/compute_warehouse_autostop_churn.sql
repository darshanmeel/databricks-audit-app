-- query_id: compute_warehouse_autostop_churn
-- title: SQL warehouse auto-stop churn - frequent auto-stops with a long wait, or frequent cold-start restarts
-- domain: compute   tier: standard
-- reads: system.compute.warehouse_events, system.compute.warehouses, system.query.history, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.compute, system.query and system.billing; GA
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :warn_long_autostop_minutes (default
--   10) auto_stop_minutes at/above which a daily auto-stop count is worth flagging; :warn_daily_autostops
--   (default 5) auto-stops in one day at/above which that day reads WARN; :crit_daily_autostops
--   (default 10) the same that reads CRITICAL; :warn_daily_restarts (default 5) STARTING events on a
--   classic or pro warehouse in one day at/above which cold_start_risk is TRUE
-- confidence: needs_confirmation
-- confidence_note: warehouse_events and query.history are already read this way by
--   compute_warehouse_idle_minutes and compute_warehouse_idle_gaps, but the auto-stop-vs-last-query
--   matching here is new. Confirm on your own account: 1) a warehouse you know auto-stops often shows
--   a matching autostop_count; 2) a classic/pro warehouse you know restarts often shows
--   cold_start_risk TRUE.
-- read_this: One row = one SQL warehouse over the window (days with no STOPPED/STARTING event are
--   not counted as observed). days_observed is how many days had at least one such event;
--   days_flagged is how many of those individually read WARN or CRITICAL by the same daily rule as
--   before. worst_day is the single worst day (ranked by status, then autostop_count, then
--   est_wasted_usd_list) with its own autostop_count and est_wasted_usd_list, so one bad day is
--   never buried in a total. total_autostop_count/total_autostop_idle_minutes/
--   total_est_wasted_usd_list sum every day in the window. cold_start_risk_days counts days a
--   classic or pro warehouse (never serverless) started at least :warn_daily_restarts times - every
--   restart pays a cold start regardless of how short the auto-stop was.
-- healthy: status = OK - no day in the window had :warn_daily_autostops or more qualifying
--   auto-stops with auto_stop_minutes at/above :warn_long_autostop_minutes.
-- investigate_if: status = the worst single day's status - WARN or CRITICAL per the same
--   :warn_daily_autostops / :crit_daily_autostops thresholds. cold_start_risk_days > 0 on its own is
--   informational, not a status band - a bursty warehouse that already auto-stops within policy
--   still needs the fix, but is not double-counted into a worse verdict here.
-- actions: 1) lower auto_stop_minutes for the warehouse - serverless can go to 5 minutes in the UI (1
--   via the API), classic/pro minimum is 10 minutes (free); 2) for cold_start_risk, move the workload
--   to serverless (starts in seconds) or consolidate several bursty jobs onto one shared warehouse so
--   it restarts less often (config).
-- next: compute_warehouse_config_posture (see auto_stop_minutes and whether it is already flagged),
--   compute_warehouse_idle_minutes (the same warehouse's idle minutes across the whole window, not
--   just at auto-stop), compute_warehouse_cache_reuse (whether staying warm actually pays off for this
--   warehouse)
-- caveats: An auto-stop is derived, not a documented event field: a STOPPED event counts only when it
--   lands at least auto_stop_minutes (minus 1 minute of rounding slack) after the later of the last
--   query finished and the warehouse's own last STARTING/RUNNING event, using system.query.history
--   within the window plus 2 days of look-back so a stop early in the window can still find its last
--   query. A STOPPED event with neither a prior query nor a prior STARTING/RUNNING event found is
--   left unclassified and excluded from autostop_count - it may be a manual stop, a forced stop, or a
--   genuine auto-stop whose last query and start both fell outside the look-back; this query does not
--   tell those apart. Each STOPPED event's idle-from time is the later (GREATEST) of two running
--   MAX(...) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY t, event_rank) columns - one
--   tracking the last query finish, one tracking the last STARTING/RUNNING event - over one timeline
--   of query-finish times, start events and stop events, not a per-stop correlated subquery
--   re-scanning all of query.history or warehouse_events. prior_start carries each warehouse's last
--   STARTING/RUNNING event from BEFORE the window into that same timeline (unbounded look-back, its
--   own true timestamp, positioned at the window start) - without it, the first stop in the window
--   whose real start event fell before it would have no last_start_at at all, and idle_wait_minutes
--   would fall back to a query that can be hours older than the actual, unseen restart. Carrying the
--   last START/RUNNING event this
--   way keeps idle time from ever crossing a stop-then-restart-then-stop boundary: a warehouse that
--   restarts on a new day with no query before its next stop is idle only since that restart, never
--   since a query on some earlier day. PRICE follows compute_warehouse_idle_minutes's own approach:
--   the warehouse's DBUs at the effective list price (DEC-66.1) divided by its up minutes over the
--   whole window gives one dollar-per-minute rate, applied to each day's own auto-stop idle minutes;
--   an unpriced or unbilled warehouse keeps its counts with a NULL dollar figure. UP MINUTES use
--   compute_warehouse_idle_minutes' own state logic: the window is [window start, today) in epoch
--   seconds, the state at the window's start is carried in from the last event before it, an event
--   tie at the same timestamp is broken by state_rank (STARTING, RUNNING, SCALED_*, STOPPING,
--   STOPPED), and a warehouse still up at the window's end is counted up to the window's end - the
--   same LEAD(...) ORDER BY (t, state_rank) segments, not a plain per-event-type LEAD with no
--   tie-break and no carry-in. cold_start_risk counts STARTING events only and never applies to
--   serverless (which has no meaningful cold start). WORST DAY - ranked by status
--   (CRITICAL, WARN, OK), then autostop_count, then est_wasted_usd_list, all descending; a tie keeps
--   the most recent day.
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
events AS (
  SELECT workspace_id, warehouse_id, event_type, cluster_count, event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= date_sub(current_date(), :period_days)
    AND event_time < current_date()
),
last_query AS (
  SELECT workspace_id, q.compute.warehouse_id AS warehouse_id,
         COALESCE(end_time, update_time, start_time) AS finished_at
  FROM system.query.history q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND start_time >= date_sub(current_date(), :period_days) - INTERVAL 48 HOURS
    AND start_time < current_date()
),
prior_start AS (
  -- the last STARTING/RUNNING event before the window, carried in at the window start - without
  -- this, the FIRST stop in the window whose real start event fell before it has no last_start_at
  -- at all and idle_wait_minutes falls back to a query that can be hours older than the actual
  -- (unseen) restart.
  SELECT workspace_id, warehouse_id, MAX(event_time) AS event_time
  FROM system.compute.warehouse_events
  WHERE event_time < date_sub(current_date(), :period_days)
    AND event_type IN ('STARTING', 'RUNNING')
  GROUP BY workspace_id, warehouse_id
),
timeline AS (
  -- one timeline per (workspace, warehouse) of query-finish times, STARTING/RUNNING events and
  -- STOPPED events, so each stop's idle-from time is a running MAX(...) OVER, not a per-stop
  -- correlated subquery.
  SELECT workspace_id, warehouse_id, finished_at AS t, finished_at AS q_finished_at,
         CAST(NULL AS TIMESTAMP) AS start_at, 0 AS event_rank
  FROM last_query
  UNION ALL
  SELECT workspace_id, warehouse_id, event_time AS t, CAST(NULL AS TIMESTAMP) AS q_finished_at,
         event_time AS start_at, 1 AS event_rank
  FROM events
  WHERE event_type IN ('STARTING', 'RUNNING')
  UNION ALL
  SELECT workspace_id, warehouse_id,
         CAST(date_sub(current_date(), :period_days) AS TIMESTAMP) AS t,
         CAST(NULL AS TIMESTAMP) AS q_finished_at,
         event_time AS start_at, 1 AS event_rank
  FROM prior_start
  UNION ALL
  SELECT workspace_id, warehouse_id, event_time AS t, CAST(NULL AS TIMESTAMP) AS q_finished_at,
         CAST(NULL AS TIMESTAMP) AS start_at, 2 AS event_rank
  FROM events
  WHERE event_type = 'STOPPED'
),
timeline_ranked AS (
  SELECT workspace_id, warehouse_id, t, event_rank,
         MAX(q_finished_at) OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY t, event_rank
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS last_query_finished_at,
         MAX(start_at) OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY t, event_rank
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS last_start_at
  FROM timeline
),
stops AS (
  SELECT workspace_id, warehouse_id, t AS stop_time, last_query_finished_at, last_start_at
  FROM timeline_ranked
  WHERE event_rank = 2
),
autostop_stops AS (
  SELECT s.workspace_id, s.warehouse_id, s.stop_time,
         (unix_timestamp(s.stop_time) - unix_timestamp(
            CASE WHEN s.last_query_finished_at IS NULL THEN s.last_start_at
                 WHEN s.last_start_at IS NULL THEN s.last_query_finished_at
                 ELSE GREATEST(s.last_query_finished_at, s.last_start_at) END
          )) / 60.0 AS idle_wait_minutes
  FROM stops s
  WHERE s.last_query_finished_at IS NOT NULL OR s.last_start_at IS NOT NULL
),
daily_events AS (
  SELECT workspace_id, warehouse_id, date_trunc('day', event_time) AS day,
         SUM(CASE WHEN event_type = 'STOPPED' THEN 1 ELSE 0 END)  AS stops_that_day,
         SUM(CASE WHEN event_type = 'STARTING' THEN 1 ELSE 0 END) AS starts_that_day
  FROM events
  GROUP BY workspace_id, warehouse_id, date_trunc('day', event_time)
),
daily_autostops AS (
  SELECT a.workspace_id, a.warehouse_id, date_trunc('day', a.stop_time) AS day,
         COUNT(*) AS autostop_count,
         SUM(a.idle_wait_minutes) AS autostop_idle_minutes
  FROM autostop_stops a
  JOIN wh w ON w.warehouse_id = a.warehouse_id
  WHERE a.idle_wait_minutes >= w.auto_stop_minutes - 1
  GROUP BY a.workspace_id, a.warehouse_id, date_trunc('day', a.stop_time)
),
-- UP MINUTES: compute_warehouse_idle_minutes' own state logic (bounds, carry-in, state_rank
-- tie-break, LEAD across the whole event set, last segment closed at the window end).
bounds AS (
  SELECT unix_timestamp(CAST(current_date() - INTERVAL :period_days DAYS AS TIMESTAMP)) AS win_start_s,
         unix_timestamp(CAST(current_date() AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
  SELECT e.workspace_id, e.warehouse_id, e.event_type, e.cluster_count,
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
  SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count
  FROM ev_all a CROSS JOIN bounds b
  WHERE a.t < b.win_start_s
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.workspace_id, a.warehouse_id
                             ORDER BY a.t DESC, a.state_rank DESC) = 1
),
wh_ev AS (
  SELECT x.*,
         CASE WHEN x.event_type IN ('STARTING', 'RUNNING', 'SCALED_UP')
                OR (x.event_type = 'SCALED_DOWN' AND COALESCE(x.cluster_count, 1) > 0)
              THEN 1 ELSE 0 END AS is_up
  FROM (
    SELECT a.workspace_id, a.warehouse_id, a.event_type, a.cluster_count, a.t, a.state_rank
    FROM ev_all a CROSS JOIN bounds b
    WHERE a.t >= b.win_start_s
    UNION ALL
    SELECT p.workspace_id, p.warehouse_id, p.event_type, p.cluster_count, b.win_start_s AS t,
           0 AS state_rank
    FROM prior p CROSS JOIN bounds b
  ) x
),
wh_seg AS (
  -- one row per event: up-state holds until the next event, or the window's end for the last one
  SELECT v.*,
         COALESCE(LEAD(v.t) OVER (PARTITION BY v.workspace_id, v.warehouse_id ORDER BY v.t, v.state_rank),
                  b.win_end_s) AS seg_end_s
  FROM wh_ev v CROSS JOIN bounds b
),
wh_up AS (
  SELECT workspace_id, warehouse_id, SUM(is_up * (seg_end_s - t)) AS up_seconds
  FROM wh_seg
  GROUP BY workspace_id, warehouse_id
),
price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
wh_cost AS (
  -- same list-price approach as compute_warehouse_idle_minutes: DBUs * effective list price
  SELECT u.usage_metadata.warehouse_id AS warehouse_id,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS usd_total
  FROM system.billing.usage u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub(current_date(), :period_days)
    AND u.usage_date < current_date()
  GROUP BY u.usage_metadata.warehouse_id
),
wh_rate AS (
  -- dollars per running minute over the whole window, applied to each day's auto-stop idle minutes
  SELECT r.warehouse_id,
         CASE WHEN r.up_seconds > 0 THEN c.usd_total / (r.up_seconds / 60.0) END AS usd_per_minute
  FROM wh_up r
  LEFT JOIN wh_cost c ON c.warehouse_id = r.warehouse_id
),
daily AS (
  -- one row per warehouse per day - the same per-day shape this query used to return, kept here as
  -- an intermediate so it can be rolled up to one row per warehouse below.
  SELECT
    d.workspace_id,
    d.warehouse_id,
    d.day,
    COALESCE(a.autostop_count, 0) AS autostop_count,
    ROUND(COALESCE(a.autostop_idle_minutes, 0), 1) AS autostop_idle_minutes,
    ROUND(COALESCE(a.autostop_idle_minutes, 0) * rt.usd_per_minute, 2) AS est_wasted_usd_list,
    (upper(w.warehouse_type) IN ('CLASSIC', 'PRO') AND d.starts_that_day >= :warn_daily_restarts) AS cold_start_risk,
    CASE
      WHEN COALESCE(a.autostop_count, 0) >= :crit_daily_autostops
           AND w.auto_stop_minutes >= :warn_long_autostop_minutes THEN 'CRITICAL'
      WHEN COALESCE(a.autostop_count, 0) >= :warn_daily_autostops
           AND w.auto_stop_minutes >= :warn_long_autostop_minutes THEN 'WARN'
      ELSE 'OK'
    END AS status
  FROM daily_events d
  -- OUTER: a warehouse with events but no compute.warehouses row (deleted before system tables
  -- started recording) still gets a day row here, same as compute_warehouse_idle_minutes' cfg.
  LEFT JOIN wh w ON w.warehouse_id = d.warehouse_id
  LEFT JOIN daily_autostops a
    ON a.workspace_id = d.workspace_id AND a.warehouse_id = d.warehouse_id AND a.day = d.day
  LEFT JOIN wh_rate rt ON rt.warehouse_id = d.warehouse_id
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY workspace_id, warehouse_id
           ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
                    autostop_count DESC, est_wasted_usd_list DESC NULLS LAST, day DESC
         ) AS worst_rank
  FROM daily
)
SELECT
  r.workspace_id,
  r.warehouse_id,
  w.warehouse_name,
  CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
       WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
       WHEN upper(w.warehouse_type) = 'CLASSIC'     THEN 'classic'
  END AS warehouse_kind,
  w.auto_stop_minutes,
  COUNT(*)                                                       AS days_observed,
  SUM(CASE WHEN d.status != 'OK' THEN 1 ELSE 0 END)               AS days_flagged,
  SUM(CASE WHEN d.cold_start_risk THEN 1 ELSE 0 END)              AS cold_start_risk_days,
  SUM(d.autostop_count)                                           AS total_autostop_count,
  ROUND(SUM(d.autostop_idle_minutes), 1)                          AS total_autostop_idle_minutes,
  ROUND(SUM(d.est_wasted_usd_list), 2)                            AS total_est_wasted_usd_list,
  r.day                                                           AS worst_day,
  r.autostop_count                                                AS worst_day_autostop_count,
  r.est_wasted_usd_list                                           AS worst_day_est_wasted_usd_list,
  r.status
FROM ranked r
LEFT JOIN wh w ON w.warehouse_id = r.warehouse_id
JOIN daily d ON d.workspace_id = r.workspace_id AND d.warehouse_id = r.warehouse_id
WHERE r.worst_rank = 1
GROUP BY r.workspace_id, r.warehouse_id, w.warehouse_name, w.warehouse_type, w.auto_stop_minutes,
         r.day, r.autostop_count, r.est_wasted_usd_list, r.status
ORDER BY CASE r.status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         total_est_wasted_usd_list DESC NULLS LAST, r.warehouse_id
