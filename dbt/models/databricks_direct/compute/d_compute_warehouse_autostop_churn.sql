{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:compute', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/app/compute/compute_warehouse_autostop_churn.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM {{ source('system_compute', 'warehouses') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
events AS (
  SELECT workspace_id, warehouse_id, event_type, cluster_count, event_time
  FROM {{ source('system_compute', 'warehouse_events') }}
  WHERE event_time >= date_sub({{ audit_today() }}, {{ w }})
    AND event_time < {{ audit_today() }}
),
last_query AS (
  SELECT workspace_id, q.compute.warehouse_id AS warehouse_id,
         COALESCE(end_time, update_time, start_time) AS finished_at
  FROM {{ source('system_query', 'history') }} q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND start_time >= date_sub({{ audit_today() }}, {{ w }}) - INTERVAL 48 HOURS
    AND start_time < {{ audit_today() }}
),
prior_start AS (
  -- the last STARTING/RUNNING event before the window, carried in at the window start - without
  -- this, the FIRST stop in the window whose real start event fell before it has no last_start_at
  -- at all and idle_wait_minutes falls back to a query that can be hours older than the actual
  -- (unseen) restart.
  SELECT workspace_id, warehouse_id, MAX(event_time) AS event_time
  FROM {{ source('system_compute', 'warehouse_events') }}
  WHERE event_time < date_sub({{ audit_today() }}, {{ w }})
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
         CAST(date_sub({{ audit_today() }}, {{ w }}) AS TIMESTAMP) AS t,
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
bounds AS (
  SELECT unix_timestamp(CAST({{ audit_today() }} - INTERVAL {{ w }} DAYS AS TIMESTAMP)) AS win_start_s,
         unix_timestamp(CAST({{ audit_today() }} AS TIMESTAMP))                            AS win_end_s
),
ev_all AS (
  SELECT e.workspace_id, e.warehouse_id, e.event_type, e.cluster_count,
         unix_timestamp(e.event_time) AS t,
         CASE e.event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                           WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                           WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank
  FROM {{ source('system_compute', 'warehouse_events') }} e
  WHERE e.event_time < {{ audit_today() }}
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
  FROM {{ list_prices() }} list_prices
),
wh_cost AS (
  -- same list-price approach as compute_warehouse_idle_minutes: DBUs * effective list price
  SELECT u.usage_metadata.warehouse_id AS warehouse_id,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS usd_total
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON  u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.warehouse_id IS NOT NULL
    AND u.usage_date >= date_sub({{ audit_today() }}, {{ w }})
    AND u.usage_date < {{ audit_today() }}
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
    (upper(w.warehouse_type) IN ('CLASSIC', 'PRO') AND d.starts_that_day >= {{ param('compute_warehouse_autostop_churn', 'warn_daily_restarts', 5) }}) AS cold_start_risk,
    CASE
      WHEN COALESCE(a.autostop_count, 0) >= {{ param('compute_warehouse_autostop_churn', 'crit_daily_autostops', 10) }}
           AND w.auto_stop_minutes >= {{ param('compute_warehouse_autostop_churn', 'warn_long_autostop_minutes', 10) }} THEN 'CRITICAL'
      WHEN COALESCE(a.autostop_count, 0) >= {{ param('compute_warehouse_autostop_churn', 'warn_daily_autostops', 5) }}
           AND w.auto_stop_minutes >= {{ param('compute_warehouse_autostop_churn', 'warn_long_autostop_minutes', 10) }} THEN 'WARN'
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
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
