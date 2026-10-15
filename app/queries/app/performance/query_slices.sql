-- query_id: query_slices
-- title: SQL warehouse statements per day, warehouse, origin, job, pipeline, notebook and query tags
-- domain: performance   tier: standard
-- reads: system.query.history, system.billing.attributed_usage, system.billing.list_prices
-- requires: SELECT on system.query and system.billing; GA
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: New app-owned query, not yet run live. Confirm that its statements, time and
--   waits per warehouse add up to perf_daily_by_resource's warehouse rows for the same window.
-- read_this: One row = one warehouse's statements on one day with the same origin, job, pipeline,
--   notebook and query tags. The Tag filter reads them to split per-warehouse numbers by each
--   query's own tags. query_tags is the statement's query_tags map as JSON. runs, failed, total_s,
--   queued_s, exec_s, slot_wait_s, provision_s, spilled_gb, shuffle_gb, read_gb, cache_read_gb,
--   result_cache_runs and fetch_s mean what they mean in perf_daily_by_resource (provision_s adds every
--   statement's own wait). attributed_dbus and attributed_usd_list are the DBUs Databricks
--   attributes to these statements (system.billing.attributed_usage) at the effective list price;
--   a warehouse's idle time is attributed to no statement, so it is not here.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (read by Queries with a Tag filter on)
-- next: perf_daily_by_resource (the same numbers per warehouse, with starts and clock waits)
-- caveats: GRAIN - usage_date, workspace_id, warehouse_id, origin, job_id, pipeline_id,
--   notebook_id, query_tags. Statements on SQL warehouses only, counted on the day they started;
--   the current day is excluded. resource_type is always 'warehouse' and resource_key is
--   workspace_id:warehouse_id, as in perf_daily_by_resource, but no warehouse is pooled.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
),
attr AS (
  SELECT a.usage_metadata.dbsql_statement_id AS statement_id,
         SUM(a.active_usage_quantity)               AS dbus,
         SUM(a.active_usage_quantity * p.list_rate) AS usd
  FROM system.billing.attributed_usage a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE a.usage_date >= current_date() - INTERVAL :period_days DAYS
    AND upper(a.usage_unit) = 'DBU'
    AND a.usage_metadata.dbsql_statement_id IS NOT NULL
  GROUP BY a.usage_metadata.dbsql_statement_id
),
stmts AS (
  SELECT q.workspace_id, q.compute.warehouse_id AS warehouse_id, date(q.start_time) AS usage_date,
         CASE WHEN q.query_source.job_info.job_id IS NOT NULL THEN 'job'
              WHEN q.query_source.pipeline_info.pipeline_id IS NOT NULL THEN 'pipeline'
              WHEN q.query_source.dashboard_id IS NOT NULL OR q.query_source.legacy_dashboard_id IS NOT NULL THEN 'dashboard'
              WHEN q.query_source.genie_space_id IS NOT NULL THEN 'genie'
              WHEN q.query_source.alert_id IS NOT NULL THEN 'alert'
              WHEN q.query_source.notebook_id IS NOT NULL THEN 'notebook'
              WHEN q.query_source.sql_query_id IS NOT NULL OR q.client_application ILIKE '%sql editor%' THEN 'sql_editor'
              WHEN q.client_application IS NOT NULL THEN 'tool'
              ELSE 'other' END AS origin,
         q.query_source.job_info.job_id AS job_id,
         q.query_source.pipeline_info.pipeline_id AS pipeline_id,
         q.query_source.notebook_id AS notebook_id,
         to_json(q.query_tags) AS query_tags,
         CASE WHEN q.execution_status = 'FAILED' THEN 1 ELSE 0 END AS failed,
         COALESCE(q.total_duration_ms, 0) / 1000.0 AS total_s,
         (COALESCE(q.waiting_at_capacity_duration_ms, 0) + COALESCE(q.waiting_for_compute_duration_ms, 0)) / 1000.0 AS queued_s,
         COALESCE(q.execution_duration_ms, 0) / 1000.0 AS exec_s,
         COALESCE(q.waiting_at_capacity_duration_ms, 0) / 1000.0 AS slot_wait_s,
         COALESCE(q.waiting_for_compute_duration_ms, 0) / 1000.0 AS provision_s,
         COALESCE(q.spilled_local_bytes, 0) / 1e9 AS spilled_gb,
         COALESCE(q.shuffle_read_bytes, 0) / 1e9 AS shuffle_gb,
         COALESCE(q.read_bytes, 0) / 1e9 AS read_gb,
         COALESCE(q.read_bytes, 0) * COALESCE(q.read_io_cache_percent, 0) / 100.0 / 1e9 AS cache_read_gb,
         CASE WHEN q.from_result_cache THEN 1 ELSE 0 END AS result_cache_runs,
         COALESCE(q.result_fetch_duration_ms, 0) / 1000.0 AS fetch_s,
         a.dbus, a.usd
  FROM system.query.history q
  LEFT JOIN attr a ON a.statement_id = q.statement_id
  WHERE q.start_time >= current_date() - INTERVAL :period_days DAYS
    AND q.start_time < current_date()
    AND q.compute.warehouse_id IS NOT NULL
)
SELECT usage_date,
       'warehouse' AS resource_type,
       concat(workspace_id, ':', warehouse_id) AS resource_key,
       workspace_id, warehouse_id, origin, job_id, pipeline_id, notebook_id, query_tags,
       COUNT(*) AS runs,
       SUM(failed) AS failed,
       ROUND(SUM(total_s), 1) AS total_s,
       ROUND(SUM(queued_s), 1) AS queued_s,
       ROUND(SUM(exec_s), 1) AS exec_s,
       ROUND(SUM(slot_wait_s), 1) AS slot_wait_s,
       ROUND(SUM(provision_s), 1) AS provision_s,
       ROUND(SUM(spilled_gb), 6) AS spilled_gb,
       ROUND(SUM(shuffle_gb), 6) AS shuffle_gb,
       ROUND(SUM(read_gb), 6) AS read_gb,
       ROUND(SUM(cache_read_gb), 6) AS cache_read_gb,
       SUM(result_cache_runs) AS result_cache_runs,
       ROUND(SUM(fetch_s), 1) AS fetch_s,
       ROUND(SUM(dbus), 6) AS attributed_dbus,
       ROUND(SUM(usd), 4) AS attributed_usd_list
FROM stmts
GROUP BY usage_date, workspace_id, warehouse_id, origin, job_id, pipeline_id, notebook_id, query_tags
ORDER BY usage_date DESC, workspace_id, warehouse_id, runs DESC
