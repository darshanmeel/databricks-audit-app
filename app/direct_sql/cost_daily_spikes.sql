-- generated from dbt/models/databricks_direct/cost/d_cost_daily_spikes.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_daily_spikes.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH daily_sku AS (
  SELECT u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name,
         SUM(u.usage_quantity * lp.list_rate) AS sku_cost,
         SUM(u.usage_quantity)                AS sku_quantity,
         MAX(lp.list_rate)                    AS list_rate
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
  WHERE u.usage_date >= dateadd(day, -(__WINDOW_DAYS__ + 14), __AS_OF_DATE__)
    AND u.usage_date <  __AS_OF_DATE__
  GROUP BY u.workspace_id, u.billing_origin_product, u.usage_date, u.sku_name
),
daily AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         SUM(sku_cost) AS day_cost,
         SUM(CASE WHEN list_rate IS NULL AND upper(sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN sku_quantity ELSE 0 END)                          AS unpriced_quantity,
         SUM(CASE WHEN list_rate IS NOT NULL THEN sku_quantity ELSE 0 END) AS priced_quantity
  FROM daily_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
),
candidates AS (
  SELECT * FROM daily WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
),
baseline_pairs AS (
  -- every earlier day, for the same workspace/product, that falls in this candidate day's own
  -- trailing 14 window (never including the candidate day itself). Null-safe keys
  -- (IS NOT DISTINCT FROM) so account-level usage (workspace_id NULL, PLAN.md 5.7) finds its own
  -- account-level baseline instead of never matching (plain `=` never matches NULL = NULL).
  SELECT c.workspace_id, c.billing_origin_product, c.usage_date AS candidate_date,
         b.day_cost AS baseline_cost
  FROM candidates c
  JOIN daily b
    ON  b.workspace_id            IS NOT DISTINCT FROM c.workspace_id
    AND b.billing_origin_product  IS NOT DISTINCT FROM c.billing_origin_product
    AND b.usage_date >= dateadd(day, -14, c.usage_date)
    AND b.usage_date <  c.usage_date
),
baseline AS (
  SELECT workspace_id, billing_origin_product, candidate_date,
         percentile(baseline_cost, 0.5) AS baseline_median_cost,
         COUNT(*)                       AS baseline_days_seen
  FROM baseline_pairs
  GROUP BY workspace_id, billing_origin_product, candidate_date
),
ranked_sku AS (
  SELECT workspace_id, billing_origin_product, usage_date, sku_name, sku_cost
  FROM daily_sku
  WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY workspace_id, billing_origin_product, usage_date ORDER BY sku_cost DESC, sku_name
  ) <= 3
),
drivers AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         array_join(collect_set(
           CASE
             WHEN sku_cost IS NULL THEN concat(sku_name,
               CASE WHEN upper(sku_name) LIKE '%FREE_USAGE%' THEN ' (free)' ELSE ' (no list price)' END)
             ELSE concat(sku_name, ' ($', CAST(CAST(ROUND(sku_cost, 0) AS BIGINT) AS STRING), ')')
           END
         ), ', ') AS top_skus
  FROM ranked_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
)
SELECT c.workspace_id,
       c.billing_origin_product,
       c.usage_date,
       ROUND(c.day_cost, 2)              AS est_day_usd_list,
       ROUND(bl.baseline_median_cost, 2) AS est_baseline_usd_list,
       COALESCE(bl.baseline_days_seen, 0) AS baseline_days_seen,
       ROUND(c.day_cost / NULLIF(bl.baseline_median_cost, 0), 2) AS spike_ratio_actual,
       d.top_skus AS top_skus,
       -- price_basis: a real $0 (free-usage SKUs) vs a pricing-coverage gap (understates the day).
       CASE
         WHEN c.unpriced_quantity > 0 THEN 'unpriced'
         WHEN c.priced_quantity = 0   THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       -- the $ floor is checked FIRST (a day that can never clear it is not worth flagging at
       -- all, priced or not), then the baseline-size cases, then the ratio itself - see caveats.
       CASE
         WHEN COALESCE(c.day_cost, 0) < 50                                  THEN 'OK'
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < 3 THEN 'NOT_ASSESSED'
         WHEN c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * 2.0          THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN COALESCE(c.day_cost, 0) < 50                    THEN NULL
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen = 0  THEN 'no_baseline_history'
         WHEN bl.baseline_days_seen < 3                 THEN 'short_baseline'
         ELSE NULL
       END AS not_assessed_reason
FROM candidates c
LEFT JOIN baseline bl
  ON  bl.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND bl.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND bl.candidate_date         = c.usage_date
LEFT JOIN drivers d
  ON  d.workspace_id           IS NOT DISTINCT FROM c.workspace_id
  AND d.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND d.usage_date             = c.usage_date
WHERE c.day_cost >= 50
  AND (
        (bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < 3)
     OR c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * 2.0
      )
ORDER BY CASE status WHEN 'CRITICAL' THEN 0 WHEN 'WARN' THEN 1 WHEN 'NOT_ASSESSED' THEN 2 ELSE 3 END,
         est_day_usd_list DESC,
         workspace_id, billing_origin_product, usage_date
) q
