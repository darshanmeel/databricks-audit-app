-- generated from dbt/models/databricks_direct/cost/d_genie_usage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/genie_usage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH snapshot AS (
  SELECT MIN(usage_date) AS snapshot_start FROM `system`.`billing`.`usage`
),
priced AS (
  SELECT u.usage_date, u.workspace_id,
         u.usage_metadata.genie.surface  AS surface,
         u.usage_metadata.genie.channel  AS channel,
         u.usage_metadata.genie.agent_id AS agent_id,
         u.identity_metadata.run_as      AS run_as_raw,
         CASE WHEN upper(u.sku_name) LIKE '%FREE%' THEN TRUE ELSE FALSE END AS is_free,
         u.usage_quantity,
         lp.list_rate
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
  WHERE u.billing_origin_product = 'GENIE'
    AND upper(u.usage_unit) = 'DBU'
    AND u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
),
agg AS (
  SELECT usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free,
         SUM(usage_quantity)                                                        AS dbus,
         -- A free-tier row is a real $0; an unpriced row adds nothing to the dollars.
         SUM(CASE WHEN is_free THEN 0 ELSE usage_quantity * list_rate END)          AS usd_list,
         SUM(CASE WHEN NOT is_free AND list_rate IS NULL THEN usage_quantity ELSE 0 END) AS unpriced_dbus
  FROM priced
  GROUP BY usage_date, workspace_id, surface, channel, agent_id, run_as_raw, is_free
)
SELECT a.usage_date,
       CASE WHEN a.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__) THEN 'current' ELSE 'previous' END AS period,
       a.workspace_id,
       a.surface,
       a.channel,
       a.agent_id,
       CASE
         WHEN a.run_as_raw IS NULL OR a.run_as_raw = '__REDACTED__' THEN 'unknown'
         WHEN a.run_as_raw LIKE '%@%' THEN 'user'
         ELSE 'service_principal'
       END AS identity_type,
       run_as_raw AS run_as,
       a.is_free,
       ROUND(a.dbus, 4) AS dbus,
       ROUND(a.usd_list, 2) AS usd_list,
       ROUND(a.unpriced_dbus, 4) AS unpriced_dbus,
       CASE
         WHEN a.is_free THEN 'free'
         WHEN a.unpriced_dbus = 0 THEN 'priced'
         WHEN a.unpriced_dbus >= a.dbus THEN 'unpriced'
         ELSE 'partly_priced'
       END AS price_basis,
       CASE WHEN s.snapshot_start <= dateadd(day, -(__WINDOW_DAYS__ * 2), __AS_OF_DATE__) THEN TRUE ELSE FALSE END AS previous_period_covered
FROM agg a
CROSS JOIN snapshot s
ORDER BY a.usage_date DESC, a.workspace_id, usd_list DESC
) q
