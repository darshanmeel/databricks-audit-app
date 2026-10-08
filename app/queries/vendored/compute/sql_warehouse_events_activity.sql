-- query_id: sql_warehouse_events_activity
-- title: SQL warehouse event activity (raw event-type breakdown)
-- domain: compute   tier: lite
-- reads: system.compute.warehouse_events, system.compute.warehouses, system.billing.usage
-- requires: SELECT on system.compute, system.billing; GA
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days; :warn_stale_days (default 7) days since a warehouse's last RUNNING/STARTING event that flags WARN; :crit_stale_days (default 14) days that flags CRITICAL
-- confidence: confirmed
-- confidence_note: Event-type enum verified against system.compute.warehouse_events in a live workspace; SCALING_UP/SCALING_DOWN values are undocumented so this query does not rely on them. The zero-event dormancy check cross-references system.billing.usage, also GA.
-- read_this: One row = one warehouse (current or not, as long as it is current or had events in the window) + event_type combination and its event count/cluster-count stats over the window - a warehouse that is current but had ZERO events at all in the window still gets exactly one row, with event_type NULL. warehouse_name is NULL when the warehouse has no current system.compute.warehouses row (deleted, or never captured by that table). The columns that matter are event_type (RUNNING/STARTING show the warehouse was actually used; SCALED_UP/SCALED_DOWN show autoscaling churn; STOPPED/STOPPING show suspend behavior; NULL means zero events) and last_event_time - for a RUNNING or STARTING row, an old last_event_time means this warehouse has gone quiet. net_dbus (the warehouse's own billed DBUs in the window) tells a genuinely dormant zero-event warehouse (billed nothing either) apart from a silent-but-still-billed one.
-- healthy: status = OK on the RUNNING/STARTING rows (last RUNNING or STARTING event within :warn_stale_days) - field heuristic; tune for your account's usage cadence.
-- investigate_if: status = CRITICAL on a NULL-event_type row (zero events AND zero billed DBUs in the window - genuinely dormant, the case this query exists to catch) or on a RUNNING/STARTING row stale for :crit_stale_days+ (WARN at :warn_stale_days+) - the warehouse has likely gone dormant. status is NOT_ASSESSED on SCALED_UP/SCALED_DOWN/STOPPED/STOPPING rows (staleness there is not inherently good or bad from this table alone), on a NULL-event_type row for a warehouse first seen within the window (no activity is expected yet), and on a NULL-event_type row that DID bill DBUs in the window (likely always-on with no scaling, not confirmably dormant from events alone).
-- actions: 1) if a warehouse shows no RUNNING/STARTING activity for a long stretch, or reads CRITICAL with zero events, confirm with its owner it is still needed before touching config (free); 2) if confirmed unused, lower auto_stop_minutes or pause/decommission it in sql_warehouse_config_current (config); 3) if it is a scheduled/rarely-used warehouse by design, move it to Serverless so idle time between runs costs nothing (spend).
-- next: sql_warehouse_config_current (to check auto_stop_minutes/warehouse_size for a dormant warehouse), compute_warehouse_idle_gaps (for the RUNNING idle-tail duration instead of just event counts), compute_warehouse_autoscale_churn (for the scaling-specific churn signal)
-- caveats: Authoritative 6-value event_type enum for system.compute.warehouse_events is SCALED_UP, SCALED_DOWN, STOPPING, RUNNING, STARTING, STOPPED. SCALING_UP/SCALING_DOWN appear in one official sample but are UNDOCUMENTED - this query does not rely on them, and you should not either. cluster_count is the number of clusters running at event time. Regional - run per metastore region. The warehouse list is the union of current (non-deleted) system.compute.warehouses rows and warehouses with events in the window, so a deleted or never-captured warehouse still shows its events; only warehouse_name and first_seen_time come from the (possibly missing) current warehouses row. first_seen_time is the earliest change_time ever seen for that warehouse_id (system.compute.warehouses has no create_time column), used only to suppress a false CRITICAL on a warehouse created inside the window (no activity is expected from it yet); a warehouse with no current row can only reach that branch if it also has zero events, which the union already excludes, so the zero-event/CRITICAL branch stays scoped to warehouses with a current row. status is only meaningful on RUNNING/STARTING rows and on the NULL-event_type (zero-event) row; it is NOT_ASSESSED on SCALED_UP/SCALED_DOWN/STOPPED/STOPPING rows since staleness on those event types does not map to a clean good/bad verdict from this table alone. A zero-event warehouse is cross-checked against its own DBU billing in the window before it can read CRITICAL: zero events but nonzero net_dbus means it is up and being charged for (likely always-on with no scaling) rather than confirmably dormant from events alone, and reads NOT_ASSESSED instead.
WITH current_warehouses AS (
  SELECT workspace_id, warehouse_id, warehouse_name, first_seen_time
  FROM (
    SELECT workspace_id, warehouse_id, warehouse_name, delete_time,
           MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id) AS first_seen_time,
           ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
    FROM system.compute.warehouses
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
  FROM system.compute.warehouse_events
  WHERE event_time >= current_timestamp() - INTERVAL :period_days DAYS
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
  FROM system.compute.warehouse_events
  WHERE event_type IN ('RUNNING', 'STARTING')
    AND event_time >= current_timestamp() - INTERVAL :period_days DAYS
  GROUP BY workspace_id, warehouse_id
),
billed AS (
  SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id,
         SUM(usage_quantity) AS net_dbus
  FROM system.billing.usage
  WHERE upper(usage_unit) = 'DBU'
    AND usage_metadata.warehouse_id IS NOT NULL
    AND usage_date >= dateadd(day, -:period_days, current_date())
    AND usage_date < current_date()
  GROUP BY workspace_id, usage_metadata.warehouse_id
)
SELECT wk.workspace_id, wk.warehouse_id, cw.warehouse_name,
       e.event_type,
       COALESCE(e.event_count, 0) AS event_count,
       e.last_event_time, e.first_event_time, e.max_cluster_count, e.avg_cluster_count,
       COALESCE(b.net_dbus, 0) AS net_dbus,
       CASE
         WHEN e.event_type IS NULL AND cw.first_seen_time >= dateadd(day, -:period_days, current_date())
              THEN 'NOT_ASSESSED'                      -- first seen within the window: no activity expected yet
         WHEN e.event_type IS NULL AND COALESCE(b.net_dbus, 0) > 0
              THEN 'NOT_ASSESSED'                       -- billed with zero events: likely always-on, not confirmably dormant
         WHEN e.event_type IS NULL THEN 'CRITICAL'      -- zero events AND zero billed DBUs: genuinely dormant
         WHEN e.event_type NOT IN ('RUNNING', 'STARTING') THEN 'NOT_ASSESSED'
         WHEN (unix_timestamp(current_timestamp()) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= :crit_stale_days THEN 'CRITICAL'
         WHEN (unix_timestamp(current_timestamp()) - unix_timestamp(re.last_running_event_time)) / 86400.0 >= :warn_stale_days THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM warehouse_keys wk
LEFT JOIN current_warehouses cw ON wk.workspace_id = cw.workspace_id AND wk.warehouse_id = cw.warehouse_id
LEFT JOIN events e          ON wk.workspace_id = e.workspace_id AND wk.warehouse_id = e.warehouse_id
LEFT JOIN running_events re ON wk.workspace_id = re.workspace_id AND wk.warehouse_id = re.warehouse_id
LEFT JOIN billed b          ON wk.workspace_id = b.workspace_id AND wk.warehouse_id = b.warehouse_id
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'OK' THEN 2 ELSE 3 END,
         last_event_time ASC NULLS FIRST
