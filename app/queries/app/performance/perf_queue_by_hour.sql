-- query_id: perf_queue_by_hour
-- title: SQL warehouse statements by day and hour: how many waited for a slot or for compute
-- domain: performance   tier: standard
-- reads: system.query.history
-- requires: SELECT on system.query; GA
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Confirm that one warehouse's runs and
--   slot_wait_s add up to its runs and slot_wait_s in perf_daily_by_resource for the same window.
-- read_this: One row = one hour of one day (UTC, by statement start) on one SQL warehouse that ran
--   at least one statement. runs counts its statements; queued_runs those that waited for a free
--   slot because the warehouse was at capacity, slot_wait_s their summed wait; provision_runs
--   those that waited for compute to start, provision_s their summed wait. queued_runs / runs is
--   the hour's queued share; the app folds days into weekdays.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory (Queries > Capacity shows it as a weekday by hour grid)
-- actions: n/a - inventory (see query_queuing_waits and query_warehouse_pressure)
-- next: query_warehouse_pressure (why a warehouse is short of slots), perf_daily_by_resource (the
--   same waits by day)
-- caveats: GRAIN - usage_date, usage_hour, warehouse_key. Statements on SQL warehouses only; hours
--   are UTC. The current day is excluded.
SELECT date(start_time) AS usage_date,
       hour(start_time) AS usage_hour,
       concat(workspace_id, ':', compute.warehouse_id) AS warehouse_key,
       workspace_id,
       compute.warehouse_id AS warehouse_id,
       COUNT(*) AS runs,
       SUM(CASE WHEN COALESCE(waiting_at_capacity_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS queued_runs,
       ROUND(SUM(COALESCE(waiting_at_capacity_duration_ms, 0)) / 1000.0, 1) AS slot_wait_s,
       SUM(CASE WHEN COALESCE(waiting_for_compute_duration_ms, 0) > 0 THEN 1 ELSE 0 END) AS provision_runs,
       ROUND(SUM(COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0, 1) AS provision_s
FROM system.query.history
WHERE start_time >= current_date() - INTERVAL :period_days DAYS
  AND start_time < current_date()
  AND compute.warehouse_id IS NOT NULL
GROUP BY date(start_time), hour(start_time), workspace_id, compute.warehouse_id
ORDER BY usage_date DESC, usage_hour, warehouse_key
