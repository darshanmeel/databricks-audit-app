-- generated from dbt/models/databricks_direct/performance/d_query_slices.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/performance/query_slices.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
),
attr AS (
  SELECT a.usage_metadata.dbsql_statement_id AS statement_id,
         SUM(a.active_usage_quantity)               AS dbus,
         SUM(a.active_usage_quantity * p.list_rate) AS usd
  FROM `system`.`billing`.`attributed_usage` a
  LEFT JOIN price p
    ON  a.sku_name = p.sku_name AND a.cloud = p.cloud AND a.usage_unit = p.usage_unit
    AND a.end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR a.end_time < p.price_end_time)
  WHERE a.usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
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
  FROM `system`.`query`.`history` q
  LEFT JOIN attr a ON a.statement_id = q.statement_id
  WHERE q.start_time >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
    AND q.start_time < __AS_OF_DATE__
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
) q
