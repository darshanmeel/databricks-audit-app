-- generated from dbt/models/databricks_direct/jobs_pipelines/d_lakeflow_job_cost_summary.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/jobs_pipelines/lakeflow_job_cost_summary.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH run_usage AS (
  -- Same per-run attribution and price join as lakeflow_job_run_cost's own run_usage CTE
  -- (usage_metadata.job_id/job_run_id, DEC-66.1 effective-list price) - see that query's header.
  SELECT u.workspace_id,
         u.usage_metadata.job_id     AS job_id,
         u.usage_metadata.job_run_id AS job_run_id,
         SUM(u.usage_quantity)                                          AS net_run_dbus,
         MAX(lp.currency_code)                                          AS currency_code,
         SUM(u.usage_quantity * lp.list_rate)                           AS net_run_list_cost,
         CASE
           WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM `system`.`billing`.`usage` u
  LEFT JOIN (
    SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
           CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
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
    ON u.sku_name = lp.sku_name
   AND u.cloud    = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_date < __AS_OF_DATE__
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.job_id IS NOT NULL
    AND u.usage_metadata.job_run_id IS NOT NULL
  GROUP BY u.workspace_id, u.usage_metadata.job_id, u.usage_metadata.job_run_id
),
job_totals AS (
  SELECT workspace_id, job_id,
         COUNT(DISTINCT job_run_id)               AS runs,
         SUM(net_run_dbus)                        AS net_job_dbus,
         SUM(net_run_list_cost)                   AS net_list_cost,
         MAX(currency_code)                       AS currency_code,
         percentile(net_run_dbus, 0.5)      AS median_run_dbus,
         MAX(net_run_dbus)                        AS max_run_dbus,
         percentile(net_run_list_cost, 0.5) AS est_median_usd_list,
         MAX(net_run_list_cost)                   AS est_max_usd_list,
         CASE
           WHEN SUM(CASE WHEN price_basis = 'unpriced' THEN 1 ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN price_basis = 'priced' THEN 1 ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM run_usage
  GROUP BY workspace_id, job_id
),
latest_jobs AS (
  -- system.lakeflow.jobs is SCD2: one row per change, so take the newest per job.
  SELECT workspace_id, job_id, name AS job_name
  FROM `system`.`lakeflow`.`jobs`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, job_id ORDER BY change_time DESC
  ) = 1
)
SELECT t.workspace_id,
       t.job_id,
       j.job_name,                                                -- NULL for submit/workflow runs
       t.runs,
       t.net_job_dbus,
       t.currency_code,
       t.net_list_cost,
       COALESCE(t.price_basis, 'priced') AS price_basis,
       t.median_run_dbus,
       t.max_run_dbus,
       t.est_median_usd_list,
       t.est_max_usd_list
FROM job_totals t
LEFT JOIN latest_jobs j
  ON  j.workspace_id = t.workspace_id
  AND j.job_id       = t.job_id
ORDER BY t.net_list_cost DESC NULLS LAST, t.net_job_dbus DESC
) q
