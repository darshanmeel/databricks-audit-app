-- generated from dbt/models/databricks_direct/cost/d_cost_sku_trend_12m.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/cost_sku_trend_12m.sql; fix the source query and regenerate, never edit this file.
SELECT 0 AS window_days, q.*
FROM (
WITH bounds AS (
  SELECT
    CAST(date_trunc('MONTH', __AS_OF_DATE__) AS DATE)                          AS cur_month_start,
    CAST(dateadd(month, -3,  date_trunc('MONTH', __AS_OF_DATE__)) AS DATE)     AS last3_start,
    CAST(dateadd(month, -6,  date_trunc('MONTH', __AS_OF_DATE__)) AS DATE)     AS prior3_start,
    CAST(dateadd(month, -9,  date_trunc('MONTH', __AS_OF_DATE__)) AS DATE)     AS mid3_end,
    CAST(dateadd(month, -12, date_trunc('MONTH', __AS_OF_DATE__)) AS DATE)     AS window_start
),
priced AS (
  SELECT u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_date,
         u.usage_quantity                    AS usage_quantity,
         u.usage_quantity * lp.list_rate     AS list_cost,
         lp.list_rate                        AS list_rate
  FROM `system`.`billing`.`usage` u
  CROSS JOIN bounds b
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
  WHERE u.usage_date >= b.window_start
    AND u.usage_date <  __AS_OF_DATE__
),
monthly AS (
  SELECT
    p.workspace_id,
    p.sku_name,
    MAX(p.billing_origin_product)                   AS billing_origin_product,
    CAST(date_trunc('MONTH', p.usage_date) AS DATE)  AS month_start,
    ROUND(SUM(p.list_cost), 2)                       AS net_list_cost_usd,
    CASE
      WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(p.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN p.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN p.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
  FROM priced p
  GROUP BY p.workspace_id, p.sku_name, CAST(date_trunc('MONTH', p.usage_date) AS DATE)
),
sku_bucket AS (
  -- per-workspace+SKU dollars in each 3-month bucket, from the raw priced rows (never from
  -- `monthly`) so a bucket with zero usage in every one of its 3 months still resolves to a real
  -- 0.0, never a NULL average over fewer months. FROM is the full `priced` set (the current
  -- partial month's own rows included, not filtered out here) so a brand-new workspace+SKU whose
  -- only usage is this month still gets a group and is not silently dropped by the later JOIN to
  -- `monthly` - each bucket's own CASE bounds it to full months only (the current month never
  -- satisfies any of the three).
  SELECT
    p.workspace_id,
    p.sku_name,
    ROUND(SUM(CASE WHEN p.usage_date >= b.last3_start AND p.usage_date < b.cur_month_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_last_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.prior3_start AND p.usage_date < b.last3_start
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_prior_3m_avg,
    ROUND(SUM(CASE WHEN p.usage_date >= b.window_start AND p.usage_date < b.mid3_end
                   THEN p.list_cost ELSE 0 END) / 3.0, 2)                        AS usd_first_3m_avg
  FROM priced p
  CROSS JOIN bounds b
  GROUP BY p.workspace_id, p.sku_name
),
sku_metrics AS (
  SELECT
    sb.workspace_id,
    sb.sku_name,
    sb.usd_last_3m_avg,
    sb.usd_prior_3m_avg,
    sb.usd_first_3m_avg,
    ROUND((sb.usd_last_3m_avg - sb.usd_prior_3m_avg) / NULLIF(sb.usd_prior_3m_avg, 0) * 100, 1) AS growth_3m_pct,
    ROUND((sb.usd_last_3m_avg - sb.usd_first_3m_avg) / NULLIF(sb.usd_first_3m_avg, 0) * 100, 1)  AS growth_12m_pct,
    -- share is within the SKU's own workspace (or within the NULL account-level group), never
    -- blended with another workspace's dollars.
    ROUND(100.0 * sb.usd_last_3m_avg
          / NULLIF(SUM(sb.usd_last_3m_avg) OVER (PARTITION BY sb.workspace_id), 0), 1)           AS share_of_total_last_3m,
    (sb.usd_first_3m_avg = 0)                                                                     AS new_this_year
  FROM sku_bucket sb
)
SELECT
    m.workspace_id,
    m.sku_name,
    m.billing_origin_product,
    CASE m.billing_origin_product
      WHEN 'ALL_PURPOSE'     THEN 'All-purpose compute'
      WHEN 'JOBS'            THEN 'Jobs'
      WHEN 'SQL'             THEN 'SQL warehouses'
      WHEN 'DLT'             THEN 'Lakeflow Declarative Pipelines'
      WHEN 'MODEL_SERVING'   THEN 'Model serving'
      WHEN 'VECTOR_SEARCH'   THEN 'Vector search'
      WHEN 'DEFAULT_STORAGE' THEN 'Storage'
      WHEN 'STORAGE'         THEN 'Storage'
      ELSE m.billing_origin_product
    END                                                       AS product_label,
    m.month_start,
    (m.month_start = b.cur_month_start)                       AS is_partial_month,
    m.net_list_cost_usd,
    m.price_basis,
    sm.usd_last_3m_avg,
    sm.usd_prior_3m_avg,
    sm.growth_3m_pct,
    sm.usd_first_3m_avg,
    sm.growth_12m_pct,
    sm.share_of_total_last_3m,
    sm.new_this_year,
    CASE
      WHEN sm.growth_3m_pct >= 100 AND sm.usd_last_3m_avg >= 1000 THEN 'CRITICAL'
      WHEN sm.growth_3m_pct >= 50 AND sm.usd_last_3m_avg >= 500  THEN 'WARN'
      WHEN sm.new_this_year AND sm.usd_last_3m_avg >= 500                   THEN 'WARN'
      ELSE 'OK'
    END AS status
FROM monthly m
CROSS JOIN bounds b
JOIN sku_metrics sm
  ON sm.sku_name = m.sku_name
 AND sm.workspace_id IS NOT DISTINCT FROM m.workspace_id
ORDER BY CASE
           WHEN sm.growth_3m_pct >= 100 AND sm.usd_last_3m_avg >= 1000 THEN 0
           WHEN sm.growth_3m_pct >= 50 AND sm.usd_last_3m_avg >= 500  THEN 1
           WHEN sm.new_this_year AND sm.usd_last_3m_avg >= 500                   THEN 1
           ELSE 2
         END,
         sm.usd_last_3m_avg DESC,
         m.workspace_id,
         m.sku_name,
         m.month_start DESC
) q
