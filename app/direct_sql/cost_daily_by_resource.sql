-- generated from dbt/models/databricks_direct/cost/d_cost_daily_by_resource.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_daily_by_resource.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH priced AS (
  SELECT u.workspace_id, u.usage_date,
         u.usage_metadata.warehouse_id    AS warehouse_id,
         u.usage_metadata.job_id          AS job_id,
         u.usage_metadata.job_run_id      AS job_run_id,
         u.usage_metadata.dlt_pipeline_id AS pipeline_id,
         u.usage_metadata.dlt_update_id   AS update_id,
         CASE WHEN u.usage_metadata.job_id IS NULL AND u.usage_metadata.dlt_pipeline_id IS NULL
              THEN u.usage_metadata.cluster_id END AS cluster_id,
         CASE WHEN u.record_type = 'ORIGINAL' THEN u.usage_start_time END AS hour_slot,
         CASE WHEN upper(u.usage_unit) = 'DBU' THEN u.usage_quantity ELSE 0 END AS dbus,
         u.usage_quantity * lp.list_rate AS list_cost,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective list price, DEC-66.1
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
  ) lp
    ON u.sku_name   = lp.sku_name
   AND u.cloud      = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
by_resource AS (
  SELECT 'workspace' AS resource_type, workspace_id, workspace_id AS resource_id, usage_date, dbus, list_cost, unpriced_quantity,
         CAST(NULL AS TIMESTAMP) AS hour_slot, CAST(NULL AS STRING) AS run_ref
  FROM priced
  UNION ALL
  SELECT 'warehouse', workspace_id, warehouse_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE warehouse_id IS NOT NULL
  UNION ALL
  SELECT 'job', workspace_id, job_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), job_run_id
  FROM priced WHERE job_id IS NOT NULL
  UNION ALL
  SELECT 'cluster', workspace_id, cluster_id, usage_date, dbus, list_cost, unpriced_quantity, hour_slot, CAST(NULL AS STRING)
  FROM priced WHERE cluster_id IS NOT NULL
  UNION ALL
  SELECT 'pipeline', workspace_id, pipeline_id, usage_date, dbus, list_cost, unpriced_quantity, CAST(NULL AS TIMESTAMP), update_id
  FROM priced WHERE pipeline_id IS NOT NULL
),
daily AS (
  SELECT resource_type, workspace_id, resource_id, usage_date,
         SUM(dbus) AS dbus, SUM(list_cost) AS list_cost, SUM(unpriced_quantity) AS unpriced_quantity,
         COUNT(DISTINCT hour_slot) AS billed_hours, COUNT(DISTINCT run_ref) AS billed_runs
  FROM by_resource
  GROUP BY resource_type, workspace_id, resource_id, usage_date
),
totals AS (
  SELECT d.*,
         SUM(COALESCE(list_cost, 0)) OVER (PARTITION BY resource_type, workspace_id, resource_id) AS total_cost,
         SUM(dbus) OVER (PARTITION BY resource_type, workspace_id, resource_id)                AS total_dbus
  FROM daily d
),
ranked AS (
  SELECT t.*,
         DENSE_RANK() OVER (PARTITION BY resource_type
                            ORDER BY total_cost DESC, total_dbus DESC,
                                     workspace_id ASC NULLS LAST, resource_id ASC NULLS LAST) AS spend_rank
  FROM totals t
),
keyed AS (
  SELECT usage_date, resource_type, workspace_id, dbus, list_cost, unpriced_quantity, billed_hours, billed_runs,
         -- Workspaces are never pooled: an account has few enough of them.
         (resource_type <> 'workspace' AND spend_rank > 100) AS is_other,
         CASE WHEN resource_type <> 'workspace' AND spend_rank > 100 THEN NULL ELSE resource_id END AS resource_id,
         CASE
           WHEN resource_type = 'workspace' THEN COALESCE(workspace_id, 'account')
           WHEN spend_rank > 100 THEN concat(COALESCE(workspace_id, 'account'), ':other')
           ELSE concat(COALESCE(workspace_id, 'account'), ':', resource_id)
         END AS resource_key
  FROM ranked
)
SELECT usage_date,
       resource_type,
       resource_key,
       workspace_id,
       resource_id,
       CASE WHEN resource_type = 'warehouse' THEN resource_id END AS warehouse_id,
       CASE WHEN resource_type = 'job' THEN resource_id END AS job_id,
       CASE WHEN resource_type = 'pipeline' THEN resource_id END AS pipeline_id,
       CASE WHEN resource_type = 'cluster' THEN resource_id END AS cluster_id,
       is_other,
       CASE WHEN is_other THEN COUNT(*) END AS pooled_count,
       ROUND(SUM(dbus), 4) AS dbus,
       -- A day of free-tier usage only is a real $0; a day with unpriced usage stays partial.
       ROUND(CASE WHEN SUM(list_cost) IS NULL AND SUM(unpriced_quantity) = 0 THEN 0 ELSE SUM(list_cost) END, 2) AS usd_list,
       ROUND(SUM(unpriced_quantity), 4) AS unpriced_quantity,
       CASE WHEN resource_type IN ('warehouse', 'cluster') THEN SUM(billed_hours) END AS billed_hours,
       CASE WHEN resource_type IN ('job', 'pipeline') THEN SUM(billed_runs) END AS billed_runs
FROM keyed
GROUP BY usage_date, resource_type, resource_key, workspace_id, resource_id, is_other
ORDER BY usage_date DESC, resource_type, resource_key
) q
