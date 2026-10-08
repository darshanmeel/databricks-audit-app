-- generated from dbt/models/databricks_direct/cost/d_cost_by_hour_of_day.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_by_hour_of_day.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
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
),
usage_priced AS (
  SELECT u.workspace_id, u.billing_origin_product,
         (datediff(DATE(u.usage_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(u.usage_start_time)             AS hour_of_day,
         u.usage_quantity * lp.list_rate      AS usd,
         CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
              THEN u.usage_quantity ELSE 0 END AS unpriced_quantity,
         CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END AS priced_quantity
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price lp
    ON  u.sku_name   = lp.sku_name
    AND u.cloud      = lp.cloud
    AND u.usage_unit = lp.usage_unit
    AND u.usage_end_time >= lp.price_start_time
    AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.usage_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND u.usage_start_time < __AS_OF_DATE__
),
spend_grid AS (
  SELECT workspace_id, weekday_num, hour_of_day, billing_origin_product,
         SUM(usd)                AS usd_list,
         SUM(unpriced_quantity)  AS unpriced_quantity,
         SUM(priced_quantity)    AS priced_quantity
  FROM usage_priced
  GROUP BY workspace_id, weekday_num, hour_of_day, billing_origin_product
),
hour_totals AS (
  -- this workspace's whole spend in this hour-of-day, across every weekday and product
  SELECT workspace_id, hour_of_day, SUM(usd_list) AS hour_usd_list
  FROM spend_grid
  GROUP BY workspace_id, hour_of_day
),
workspace_totals AS (
  -- this workspace's whole window spend, every weekday, hour and product
  SELECT workspace_id, SUM(usd_list) AS window_usd_list
  FROM spend_grid
  GROUP BY workspace_id
),
job_runs AS (
  SELECT workspace_id,
         (datediff(DATE(period_start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(period_start_time)                                     AS hour_of_day,
         COUNT(DISTINCT CONCAT(job_id, '::', run_id))                AS job_runs_active
  FROM `system`.`lakeflow`.`job_run_timeline`
  WHERE period_start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND period_start_time < __AS_OF_DATE__
  GROUP BY workspace_id, weekday_num, hour_of_day
),
query_counts AS (
  SELECT workspace_id,
         (datediff(DATE(start_time), DATE '2023-01-02') % 7) AS weekday_num,
         hour(start_time)                                     AS hour_of_day,
         COUNT(*)                                              AS queries_started
  FROM `system`.`query`.`history`
  WHERE start_time >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
    AND start_time < __AS_OF_DATE__
  GROUP BY workspace_id, weekday_num, hour_of_day
)
SELECT g.workspace_id,
       CASE g.weekday_num
         WHEN 0 THEN 'Monday'    WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday'
         WHEN 3 THEN 'Thursday'  WHEN 4 THEN 'Friday'  WHEN 5 THEN 'Saturday'
         ELSE 'Sunday'
       END                                                  AS weekday,
       g.weekday_num,
       g.hour_of_day,
       g.billing_origin_product,
       ROUND(g.usd_list, 2)                                 AS usd_list,
       CASE
         WHEN g.unpriced_quantity > 0 THEN 'unpriced'
         WHEN g.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END                                                  AS price_basis,
       COALESCE(jr.job_runs_active, 0)                      AS job_runs_active,
       COALESCE(qc.queries_started, 0)                      AS queries_started,
       ROUND(ht.hour_usd_list, 2)                           AS hour_usd_list,
       ROUND(wt.window_usd_list, 2)                         AS window_usd_list,
       ROUND(100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0), 1) AS share_of_window_spend,
       CASE
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= 25 THEN 'CRITICAL'
         WHEN 100.0 * ht.hour_usd_list / NULLIF(wt.window_usd_list, 0) >= 15 THEN 'WARN'
         ELSE 'OK'
       END                                                  AS status
FROM spend_grid g
LEFT JOIN hour_totals ht
  ON  ht.workspace_id IS NOT DISTINCT FROM g.workspace_id AND ht.hour_of_day = g.hour_of_day
LEFT JOIN workspace_totals wt
  ON  wt.workspace_id IS NOT DISTINCT FROM g.workspace_id
LEFT JOIN job_runs jr
  ON  jr.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND jr.weekday_num = g.weekday_num AND jr.hour_of_day = g.hour_of_day
LEFT JOIN query_counts qc
  ON  qc.workspace_id IS NOT DISTINCT FROM g.workspace_id
  AND qc.weekday_num = g.weekday_num AND qc.hour_of_day = g.hour_of_day
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         g.workspace_id, g.weekday_num, g.hour_of_day, g.billing_origin_product
) q
