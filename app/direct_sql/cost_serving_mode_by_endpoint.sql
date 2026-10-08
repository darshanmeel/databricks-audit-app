-- generated from dbt/models/databricks_direct/cost/d_cost_serving_mode_by_endpoint.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_serving_mode_by_endpoint.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT
  u.usage_date                           AS usage_date,
  u.workspace_id                         AS workspace_id,
  u.usage_metadata.endpoint_id           AS endpoint_id,
  u.usage_metadata.endpoint_name AS endpoint_name,
  u.cloud                                AS cloud,
  u.sku_name                             AS sku_name,
  u.usage_type                           AS usage_type,
  u.usage_unit                           AS usage_unit,
  u.product_features.serving_type        AS serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%'
       THEN TRUE ELSE FALSE END          AS is_launch_sku,
  SUM(u.usage_quantity)                  AS net_usage_quantity,
  SUM(u.usage_quantity * lp.list_rate)   AS net_list_cost,
  -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END                                     AS price_basis,
  -- status: est_usd_list/day band per endpoint + day + usage_type (field heuristic; 50 / 250).
  -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
  -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
  CASE
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
    WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= 250 THEN 'CRITICAL'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= 50 THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM `system`.`billing`.`usage` u
LEFT JOIN (
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
) lp
  ON  u.sku_name = lp.sku_name
  AND u.cloud    = lp.cloud
  AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND u.usage_date <  __AS_OF_DATE__
  AND u.billing_origin_product = 'MODEL_SERVING'
GROUP BY
  u.usage_date,
  u.workspace_id,
  u.usage_metadata.endpoint_id,
  u.usage_metadata.endpoint_name,
  u.cloud,
  u.sku_name,
  u.usage_type,
  u.usage_unit,
  u.product_features.serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%' THEN TRUE ELSE FALSE END,
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END
ORDER BY net_list_cost DESC NULLS LAST
) q
