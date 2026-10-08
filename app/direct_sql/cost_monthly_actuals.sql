-- generated from dbt/models/databricks_direct/cost/d_cost_monthly_actuals.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_monthly_actuals.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH snapshot AS (
  -- the WHOLE account's own earliest recorded usage_date across system.billing.usage - the
  -- DEC-64 signal for whether the export's own oldest month is itself only partly captured.
  SELECT MIN(usage_date) AS snapshot_start
  FROM `system`.`billing`.`usage`
)
SELECT
    u.workspace_id,
    u.billing_origin_product,
    CAST(date_trunc('MONTH', u.usage_date) AS DATE)               AS month_start,
    MAX(lp.currency_code)                                         AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2)                AS net_list_cost_usd,
    COUNT(DISTINCT u.usage_date)                                  AS days_captured,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', __AS_OF_DATE__) AS DATE)
        THEN TRUE
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN TRUE
      ELSE FALSE
    END AS is_partial_month,
    CASE
      WHEN CAST(date_trunc('MONTH', u.usage_date) AS DATE) = CAST(date_trunc('MONTH', __AS_OF_DATE__) AS DATE)
        THEN 'month_in_progress'
      WHEN s.snapshot_start > CAST(date_trunc('MONTH', u.usage_date) AS DATE)
        THEN 'export_starts_mid_month'
      ELSE NULL
    END AS partial_reason,
    -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the month).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis,
    MIN(u.usage_date)                                             AS first_day,
    MAX(u.usage_date)                                             AS last_day
FROM `system`.`billing`.`usage` u
CROSS JOIN snapshot s
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
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
WHERE u.usage_date < __AS_OF_DATE__
GROUP BY u.workspace_id, u.billing_origin_product, CAST(date_trunc('MONTH', u.usage_date) AS DATE),
         s.snapshot_start
ORDER BY month_start DESC, workspace_id, billing_origin_product
) q
