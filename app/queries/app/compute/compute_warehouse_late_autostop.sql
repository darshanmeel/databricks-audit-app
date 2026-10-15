-- query_id: compute_warehouse_late_autostop
-- title: SQL warehouse late auto-stop - stopped well after its auto-stop time following the last query
-- domain: compute   tier: standard
-- reads: system.compute.warehouse_events, system.compute.warehouses, system.query.history
-- requires: SELECT on system.compute and system.query; GA
-- empty_if: schema_not_enabled, no_activity
-- params: :period_days (default 30) rolling window in days; :late_pct (default 20) a stop is late
--   when the wait from the last query to the stop is more than this percent longer than the
--   auto-stop setting; :min_late_seconds (default 60) and at least this many seconds longer, so a
--   1-minute auto-stop is not flagged for a few seconds of lag; :warn_late_stops (default 3) late
--   stops in the window at/above which the warehouse reads WARN
-- confidence: needs_confirmation
-- confidence_note: Reads warehouse_events and query.history the way compute_warehouse_idle_minutes
--   and compute_warehouse_autostop_churn do. Confirm on your account: for the longest late stop, run
--   the audit-log query in the caveats over last_activity_at to stop_at; no rows there means nothing
--   recorded kept the warehouse up.
-- read_this: One row = one SQL warehouse with auto-stop on that auto-stopped in the window. A stop is
--   the warehouse going from up to STOPPING or STOPPED. Its wait runs from the last activity before
--   it (the later of the last statement's end in system.query.history, after its client fetched the
--   result, and the last STARTING or RUNNING event) to the stop, judged against the auto-stop setting in force at that time.
--   auto_stops counts stops that waited at least the setting minus one minute (shorter ones are
--   manual or forced stops). typical_wait_minutes is their median wait. late_stops waited more than
--   :late_pct percent and more than :min_late_seconds seconds longer than the setting;
--   late_extra_minutes sums the minutes past the setting. longest_wait_minutes, last_activity_at and
--   stop_at give the latest stop, to check in the audit log.
-- healthy: status = OK - fewer than :warn_late_stops late stops in the window.
-- investigate_if: status = WARN - :warn_late_stops or more late stops. Either something query history
--   does not record kept the warehouse up, or Databricks stopped it late. Never CRITICAL: the app
--   cannot tell which.
-- actions: 1) run the audit-log query in the caveats for the longest late stop; rows there name
--   what kept it up, such as a BI tool's metadata calls or a client fetching results (config);
--   2) with no rows there, raise it with Databricks support with the warehouse, its auto-stop and
--   the last_activity_at and stop_at times (support).
-- next: compute_warehouse_idle_minutes (prices this waiting time as part of the idle minutes),
--   compute_warehouse_autostop_churn (how often it auto-stops)
-- caveats: BASED ON QUERY HISTORY: activity it does not record can keep a warehouse up, so a late
--   stop is a lead to check, not proof that Databricks stopped late. To check one stop, run SELECT
--   event_time, service_name, action_name, request_params FROM system.access.audit WHERE event_time
--   BETWEEN last_activity_at AND stop_at AND to_json(request_params) LIKE '%<warehouse_id>%'. A
--   statement still running at the stop means the stop was not an auto-stop, and that stop is left
--   out. Statements come from the window plus 2 days before it, so a stop early in the window finds
--   its last query. When no setting row is older than the stop, the earliest recorded setting is
--   used. No dollar figure: compute_warehouse_idle_minutes already prices this time, and repeating it
--   here would count it twice. Regional: warehouse_events and query.history hold the snapshot's
--   region only.
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
ev AS (
  -- documented events from 2 days before the window, so the first stop has its start and state
  SELECT workspace_id, warehouse_id, event_type, event_time AS ts,
         CASE event_type WHEN 'STARTING' THEN 1 WHEN 'RUNNING' THEN 2
                         WHEN 'SCALED_UP' THEN 3 WHEN 'SCALED_DOWN' THEN 3
                         WHEN 'STOPPING' THEN 4 ELSE 5 END AS state_rank,
         CASE WHEN event_type IN ('STOPPING', 'STOPPED')
                OR (event_type = 'SCALED_DOWN' AND COALESCE(cluster_count, 1) = 0)
              THEN 0 ELSE 1 END AS is_up
  FROM system.compute.warehouse_events
  WHERE event_time >= date_sub(current_date(), :period_days) - INTERVAL 48 HOURS
    AND event_time < current_date()
    AND event_type IN ('STARTING', 'RUNNING', 'SCALED_UP', 'SCALED_DOWN', 'STOPPING', 'STOPPED')
),
ev_seq AS (
  -- a stop is the first down event after an up one
  SELECT e.*,
         e.is_up = 0 AND LAG(e.is_up) OVER (PARTITION BY e.workspace_id, e.warehouse_id
                                            ORDER BY e.ts, e.state_rank) = 1 AS is_stop
  FROM ev e
),
timeline AS (
  -- statements at their start carrying their end, and every state event; a running MAX over it
  -- gives each stop its last activity without a per-stop scan
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, q.start_time AS ts, 0 AS state_rank,
         COALESCE(dateadd(MILLISECOND, COALESCE(q.result_fetch_duration_ms, 0), q.end_time), q.update_time, q.start_time) AS stmt_end,
         CAST(NULL AS TIMESTAMP) AS start_ts, FALSE AS is_stop
  FROM system.query.history q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= date_sub(current_date(), :period_days) - INTERVAL 48 HOURS
    AND q.start_time < current_date()
  UNION ALL
  SELECT workspace_id, warehouse_id, ts, state_rank, CAST(NULL AS TIMESTAMP) AS stmt_end,
         CASE WHEN event_type IN ('STARTING', 'RUNNING') THEN ts END AS start_ts,
         COALESCE(is_stop, FALSE) AS is_stop
  FROM ev_seq
),
ordered AS (
  SELECT workspace_id, warehouse_id, ts, is_stop,
         MAX(stmt_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY ts, state_rank
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_end,
         MAX(start_ts) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY ts, state_rank
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_start
  FROM timeline
),
stops AS (
  -- a statement still running at the stop means it was not an auto-stop
  SELECT workspace_id, warehouse_id, ts AS stop_at,
         GREATEST(last_end, last_start) AS idle_from
  FROM ordered
  WHERE is_stop
    AND ts >= CAST(date_sub(current_date(), :period_days) AS TIMESTAMP)
    AND GREATEST(last_end, last_start) IS NOT NULL
    AND (last_end IS NULL OR last_end < ts)
),
judged AS (
  -- the auto-stop setting in force at each stop, else the earliest recorded one
  SELECT s.workspace_id, s.warehouse_id, s.stop_at, s.idle_from,
         unix_timestamp(s.stop_at) - unix_timestamp(s.idle_from) AS wait_s,
         h.auto_stop_minutes
  FROM stops s
  JOIN system.compute.warehouses h ON h.warehouse_id = s.warehouse_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.workspace_id, s.warehouse_id, s.stop_at
    ORDER BY CASE WHEN h.change_time <= s.stop_at THEN 0 ELSE 1 END,
             CASE WHEN h.change_time <= s.stop_at THEN h.change_time END DESC,
             h.change_time) = 1
),
autostops AS (
  SELECT j.*,
         j.wait_s > j.auto_stop_minutes * 60 * (1 + :late_pct / 100.0)
           AND j.wait_s - j.auto_stop_minutes * 60 > :min_late_seconds AS is_late
  FROM judged j
  WHERE j.auto_stop_minutes > 0
    AND j.wait_s >= j.auto_stop_minutes * 60 - 60
),
longest AS (
  SELECT workspace_id, warehouse_id, wait_s, idle_from, stop_at
  FROM autostops
  WHERE is_late
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY wait_s DESC, stop_at DESC) = 1
),
agg AS (
  SELECT workspace_id, warehouse_id,
         COUNT(*) AS auto_stops,
         percentile(wait_s, 0.5) AS typical_wait_s,
         SUM(CASE WHEN is_late THEN 1 ELSE 0 END) AS late_stops,
         SUM(CASE WHEN is_late THEN wait_s - auto_stop_minutes * 60 ELSE 0 END) AS late_extra_s
  FROM autostops
  GROUP BY workspace_id, warehouse_id
)
SELECT a.workspace_id,
       a.warehouse_id,
       w.warehouse_name,
       CASE WHEN upper(w.warehouse_type) = 'SERVERLESS' THEN 'serverless'
            WHEN upper(w.warehouse_type) = 'PRO'        THEN 'pro'
            WHEN upper(w.warehouse_type) = 'CLASSIC'    THEN 'classic'
       END                                                   AS warehouse_kind,
       w.auto_stop_minutes,
       a.auto_stops,
       ROUND(a.typical_wait_s / 60.0, 1)                     AS typical_wait_minutes,
       a.late_stops,
       ROUND(a.late_extra_s / 60.0, 1)                       AS late_extra_minutes,
       ROUND(l.wait_s / 60.0, 1)                             AS longest_wait_minutes,
       l.idle_from                                           AS last_activity_at,
       l.stop_at,
       CASE WHEN a.late_stops = 0
            THEN CONCAT('all ', a.auto_stops, ' auto-stops within ', :late_pct, '% of the setting')
            ELSE CONCAT(a.late_stops, ' of ', a.auto_stops, ' auto-stops came more than ', :late_pct,
                        '% after the setting (', CAST(ROUND(a.late_extra_s / 60.0, 0) AS BIGINT),
                        ' extra min); longest ', CAST(ROUND(l.wait_s / 60.0, 1) AS STRING), ' min')
       END                                                   AS late_reason,
       CASE WHEN a.late_stops >= :warn_late_stops THEN 'WARN' ELSE 'OK' END AS status
FROM agg a
LEFT JOIN wh w      ON w.warehouse_id = a.warehouse_id
LEFT JOIN longest l ON l.workspace_id = a.workspace_id AND l.warehouse_id = a.warehouse_id
ORDER BY CASE WHEN a.late_stops >= :warn_late_stops THEN 0 ELSE 1 END,
         late_extra_minutes DESC, a.warehouse_id
