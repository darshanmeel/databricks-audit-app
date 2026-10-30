-- generated from dbt/models/databricks_direct/compute/d_compute_warehouse_late_autostop.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/compute/compute_warehouse_late_autostop.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, auto_stop_minutes
  FROM `system`.`compute`.`warehouses`
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
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__) - INTERVAL 48 HOURS
    AND event_time < __AS_OF_DATE__
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
  FROM `system`.`query`.`history` q
  WHERE q.compute.warehouse_id IS NOT NULL
    AND q.start_time >= date_sub(__AS_OF_DATE__, __WINDOW_DAYS__) - INTERVAL 48 HOURS
    AND q.start_time < __AS_OF_DATE__
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
    AND ts >= CAST(date_sub(__AS_OF_DATE__, __WINDOW_DAYS__) AS TIMESTAMP)
    AND GREATEST(last_end, last_start) IS NOT NULL
    AND (last_end IS NULL OR last_end < ts)
),
judged AS (
  -- the auto-stop setting in force at each stop, else the earliest recorded one
  SELECT s.workspace_id, s.warehouse_id, s.stop_at, s.idle_from,
         unix_timestamp(s.stop_at) - unix_timestamp(s.idle_from) AS wait_s,
         h.auto_stop_minutes
  FROM stops s
  JOIN `system`.`compute`.`warehouses` h ON h.warehouse_id = s.warehouse_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.workspace_id, s.warehouse_id, s.stop_at
    ORDER BY CASE WHEN h.change_time <= s.stop_at THEN 0 ELSE 1 END,
             CASE WHEN h.change_time <= s.stop_at THEN h.change_time END DESC,
             h.change_time) = 1
),
autostops AS (
  SELECT j.*,
         j.wait_s > j.auto_stop_minutes * 60 * (1 + 20 / 100.0)
           AND j.wait_s - j.auto_stop_minutes * 60 > 60 AS is_late
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
            THEN CONCAT('all ', a.auto_stops, ' auto-stops within ', 20, '% of the setting')
            ELSE CONCAT(a.late_stops, ' of ', a.auto_stops, ' auto-stops came more than ', 20,
                        '% after the setting (', CAST(ROUND(a.late_extra_s / 60.0, 0) AS BIGINT),
                        ' extra min); longest ', CAST(ROUND(l.wait_s / 60.0, 1) AS STRING), ' min')
       END                                                   AS late_reason,
       CASE WHEN a.late_stops >= 3 THEN 'WARN' ELSE 'OK' END AS status
FROM agg a
LEFT JOIN wh w      ON w.warehouse_id = a.warehouse_id
LEFT JOIN longest l ON l.workspace_id = a.workspace_id AND l.warehouse_id = a.warehouse_id
ORDER BY CASE WHEN a.late_stops >= 3 THEN 0 ELSE 1 END,
         late_extra_minutes DESC, a.warehouse_id
) q
