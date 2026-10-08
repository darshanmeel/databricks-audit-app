-- generated from dbt/models/databricks_direct/compute/d_sql_warehouse_events_activity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/sql_warehouse_events_activity.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH current_warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name, first_seen_time
  FROM (
    SELECT workspace_id, warehouse_id, warehouse_name, delete_time,
           MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id) AS first_seen_time,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
    FROM `system`.`compute`.`warehouses`
  )
  WHERE rn = 1 AND delete_time IS NULL
),
events AS (
  SELECT workspace_id, warehouse_id, event_type,
         COUNT(*)           AS event_count,
         MAX(event_time)    AS last_event_time,
         MIN(event_time)    AS first_event_time,
         MAX(cluster_count) AS max_cluster_count,
         AVG(cluster_count) AS avg_cluster_count
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_time >= __AS_OF_TS__ - INTERVAL __WINDOW_DAYS__ DAYS
  GROUP BY workspace_id, warehouse_id, event_type
),
warehouse_keys AS (
  -- current warehouses plus any warehouse with events, so a deleted/never-captured one still shows
  SELECT workspace_id, warehouse_id FROM current_warehouses
  UNION
  SELECT workspace_id, warehouse_id FROM events
),
running_events AS (
  SELECT workspace_id, warehouse_id, MAX(event_time) AS last_running_event_time
  FROM `system`.`compute`.`warehouse_events`
  WHERE event_type IN ('RUNNING', 'STARTING')
    AND event_time >= __AS_OF_TS__ - INTERVAL __WINDOW_DAYS__ DAYS
  GROUP BY workspace_id, warehouse_id
),
billed AS (
  SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id,
         SUM(usage_quantity) AS net_dbus
  FROM `system`.`billing`.`usage`
  WHERE upper(usage_unit) = 'DBU'
    AND usage_metadata.warehouse_id IS NOT NULL
    AND usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND usage_date < __AS_OF_DATE__
  GROUP BY workspace_id, usage_metadata.warehouse_id
)
SELECT wk.workspace_id, wk.warehouse_id, cw.warehouse_name,
       e.event_type,
       COALESCE(e.event_count, 0) AS event_count,
       e.last_event_time, e.first_event_time, e.max_cluster_count, e.avg_cluster_count,
       COALESCE(b.net_dbus, 0) AS net_dbus,
       CASE
         WHEN e.event_type IS NULL AND cw.first_seen_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
              THEN 'NOT_ASSESSED'                      -- first seen within the window: no activity expected yet
         WHEN e.event_type IS NULL AND COALESCE(b.net_dbus, 0) > 0
              THEN 'NOT_ASSESSED'                       -- billed with zero events: likely always-on, not confirmably dormant
         WHEN e.event_type IS NULL THEN 'CRITICAL'      -- zero events AND zero billed DBUs: genuinely dormant
         WHEN e.event_type NOT IN ('RUNNING', 'STARTING') THEN 'NOT_ASSESSED'
         WHEN (unix_timestamp(__AS_OF_TS__) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= 14 THEN 'CRITICAL'
         WHEN (unix_timestamp(__AS_OF_TS__) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= 7 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM warehouse_keys wk
LEFT JOIN current_warehouses cw ON wk.workspace_id = cw.workspace_id AND wk.warehouse_id = cw.warehouse_id
LEFT JOIN events e          ON wk.workspace_id = e.workspace_id AND wk.warehouse_id = e.warehouse_id
LEFT JOIN running_events re ON wk.workspace_id = re.workspace_id AND wk.warehouse_id = re.warehouse_id
LEFT JOIN billed b          ON wk.workspace_id = b.workspace_id AND wk.warehouse_id = b.warehouse_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         last_event_time ASC NULLS FIRST
) q
