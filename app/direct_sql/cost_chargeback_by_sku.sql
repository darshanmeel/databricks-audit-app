-- generated from dbt/models/databricks_direct/cost/d_cost_chargeback_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_chargeback_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.sku_name, u.cloud, u.usage_unit, u.workspace_id, u.usage_date,
         u.usage_quantity                AS usage_quantity,
         u.usage_quantity * lp.list_rate AS list_cost,
         lp.list_rate                    AS list_rate
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
  WHERE u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
raw_agg AS (
  SELECT sku_name, cloud, usage_unit, workspace_id,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                        AS current_cost,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                  THEN list_cost END)                                        AS previous_cost,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND upper(usage_unit) = 'DBU'
                  THEN usage_quantity END)                                   AS current_dbus,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS current_unpriced_quantity,
         SUM(CASE WHEN usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS current_priced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN usage_quantity ELSE 0 END)                            AS previous_unpriced_quantity,
         SUM(CASE WHEN usage_date <  dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
                   AND list_rate IS NOT NULL
                  THEN usage_quantity ELSE 0 END)                            AS previous_priced_quantity
  FROM priced
  GROUP BY sku_name, cloud, usage_unit, workspace_id
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (
  SELECT SUM(eff_current_cost) AS total_usd_list FROM agg
)
SELECT a.workspace_id,
       a.sku_name,
       replace(lower(a.sku_name), '_', ' ')                               AS sku_name_readable,
       a.cloud,
       a.usage_unit,
       ROUND(a.eff_current_cost, 2)                                      AS usd_list,
       ROUND(a.current_dbus, 2)                                          AS dbus,
       ROUND(a.eff_current_cost * 100.0 / NULLIF(t.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(a.eff_previous_cost, 2)                                     AS prev_usd_list,
       ROUND(a.eff_current_cost - a.eff_previous_cost, 2)                AS change_usd_list,
       ROUND((a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (a.current_unpriced_quantity + a.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (a.current_priced_quantity + a.previous_priced_quantity) = 0     THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__) THEN 'NOT_ASSESSED'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'NOT_ASSESSED'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'NOT_ASSESSED'
         WHEN COALESCE(a.eff_current_cost, 0) < 20                    THEN 'OK'
         WHEN a.eff_previous_cost = 0                                             THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 50
           THEN 'CRITICAL'
         WHEN (a.eff_current_cost - a.eff_previous_cost) / NULLIF(a.eff_previous_cost, 0) * 100 >= 25
           THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN s.snapshot_start > dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__) THEN 'previous_window_not_covered'
         WHEN a.current_cost IS NULL AND a.current_unpriced_quantity > 0           THEN 'current_period_unpriced'
         WHEN a.previous_cost IS NULL AND a.previous_unpriced_quantity > 0         THEN 'previous_period_unpriced'
         ELSE NULL
       END AS not_assessed_reason
FROM agg a
CROSS JOIN snapshot s
CROSS JOIN total t
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         a.workspace_id,
         change_usd_list DESC NULLS LAST,
         a.sku_name, a.cloud, a.usage_unit
) q
