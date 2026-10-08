-- generated from dbt/models/databricks_direct/cost/d_cost_unnamed_workspaces.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_unnamed_workspaces.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH priced AS (
  SELECT u.workspace_id, u.usage_date, u.usage_unit, u.usage_quantity,
         u.usage_quantity * p.list_rate AS list_cost
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
  ) p
    ON  u.sku_name   = p.sku_name
    AND u.cloud      = p.cloud
    AND u.usage_unit = p.usage_unit
    AND u.usage_end_time >= p.price_start_time
    AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.workspace_id IS NOT NULL
    AND datediff(__AS_OF_DATE__, u.usage_date) <= 365
    AND u.usage_date < __AS_OF_DATE__
),
by_ws AS (
  SELECT workspace_id,
         MIN(usage_date)                                                          AS first_used,
         MAX(usage_date)                                                          AS last_used,
         COUNT(DISTINCT usage_date)                                               AS active_days,
         SUM(CASE WHEN upper(usage_unit) = 'DBU' THEN usage_quantity ELSE 0 END)  AS dbus_365d,
         SUM(list_cost)                                                           AS net_list_cost_usd_365d,
         SUM(CASE WHEN usage_date >= __AS_OF_DATE__ - INTERVAL __WINDOW_DAYS__ DAYS
                  THEN list_cost END)                                             AS window_cost
  FROM priced
  GROUP BY workspace_id
)
SELECT
  b.workspace_id,
  b.first_used,
  b.last_used,
  b.active_days,
  ROUND(b.dbus_365d, 2)                          AS dbus_365d,
  ROUND(b.net_list_cost_usd_365d, 2)             AS net_list_cost_usd_365d,
  ROUND(COALESCE(b.window_cost, 0), 2)           AS net_list_cost_usd,
  datediff(b.last_used, b.first_used) + 1        AS lifetime_days,
  (datediff(b.last_used, b.first_used) + 1) < 30 AS short_lived,
  CASE
    WHEN COALESCE(b.window_cost, 0) > 0              THEN 'CRITICAL'
    WHEN datediff(__AS_OF_DATE__, b.last_used) <= 90 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM by_ws b
LEFT JOIN `system`.`access`.`workspaces_latest` wl ON wl.workspace_id = b.workspace_id
WHERE wl.workspace_id IS NULL
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
         net_list_cost_usd DESC NULLS LAST, b.last_used DESC, b.workspace_id
) q
