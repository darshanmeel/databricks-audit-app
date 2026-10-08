-- generated from dbt/models/databricks_direct/cost/d_cost_networking_egress.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_networking_egress.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.source_region      AS source_region,
       u.usage_metadata.destination_region AS destination_region,
       u.usage_metadata.networking_client  AS networking_client,
       u.usage_metadata.recipient_id       AS recipient_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM `system`.`billing`.`usage` u
LEFT JOIN (
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
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND u.usage_date < __AS_OF_DATE__
  AND u.usage_type IN ('NETWORK_BYTE', 'NETWORK_HOUR')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.source_region, u.usage_metadata.destination_region,
         u.usage_metadata.networking_client, u.usage_metadata.recipient_id
ORDER BY usage_date DESC, cloud, usage_type
) q
