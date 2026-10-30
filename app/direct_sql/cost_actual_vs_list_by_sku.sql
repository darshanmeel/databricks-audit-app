-- generated from dbt/models/databricks_direct/cost/d_cost_actual_vs_list_by_sku.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_actual_vs_list_by_sku.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product,
       SUM(u.usage_quantity)                       AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate)        AS net_list_cost,
       -- status: this check can never be assessed - no negotiated-rate source exists anywhere in
       -- system.billing, so every row reads NOT_ASSESSED instead of a fabricated realization band.
       'NOT_ASSESSED'                                                      AS status,
       'no_negotiated_rate_source'                                        AS not_assessed_reason
FROM `system`.`billing`.`usage` u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1's basis
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
  ON  u.sku_name      = lp.sku_name
  AND u.cloud         = lp.cloud
  AND u.usage_unit    = lp.usage_unit
  AND u.usage_date    >= DATE(lp.price_start_time)
  AND (lp.price_end_time IS NULL OR u.usage_date < DATE(lp.price_end_time))   -- active rows carry NULL end_time
WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND u.usage_date < __AS_OF_DATE__
  AND upper(u.usage_unit) = 'DBU'   -- price only DBU rows against a per-DBU rate; never blend bytes/hours/tokens
GROUP BY u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product
ORDER BY net_list_cost DESC NULLS LAST
) q
