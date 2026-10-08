-- cost_sku_trend_12m over the dollars a tag filter keeps (app/core/tag_spend.py); same months,
-- thresholds and status rules as the check itself.
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day),
bounds AS (
  SELECT a.d,
         CAST(date_trunc('MONTH', a.d) AS DATE) AS cur_month_start,
         CAST(date_trunc('MONTH', a.d) - INTERVAL 3 MONTH AS DATE) AS last3_start,
         CAST(date_trunc('MONTH', a.d) - INTERVAL 6 MONTH AS DATE) AS prior3_start,
         CAST(date_trunc('MONTH', a.d) - INTERVAL 9 MONTH AS DATE) AS mid3_end,
         CAST(date_trunc('MONTH', a.d) - INTERVAL 12 MONTH AS DATE) AS window_start
  FROM snap a
),
priced AS (
  SELECT t.* FROM tagged t, bounds b WHERE t.usage_date >= b.window_start AND t.usage_date < b.d
),
monthly AS (
  SELECT p.workspace_id, p.sku_name, MAX(p.billing_origin_product) AS billing_origin_product,
         CAST(date_trunc('MONTH', p.usage_date) AS DATE) AS month_start,
         ROUND(SUM(p.usd), 2) AS net_list_cost_usd,
         CASE
           WHEN SUM(CASE WHEN upper(p.sku_name) NOT LIKE '%FREE_USAGE%' THEN p.unpriced_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(p.quantity - p.unpriced_quantity) = 0 THEN 'free'
           ELSE 'priced'
         END AS price_basis
  FROM priced p
  GROUP BY p.workspace_id, p.sku_name, CAST(date_trunc('MONTH', p.usage_date) AS DATE)
),
sku_bucket AS (
  SELECT p.workspace_id, p.sku_name,
         ROUND(SUM(CASE WHEN p.usage_date >= b.last3_start AND p.usage_date < b.cur_month_start THEN p.usd ELSE 0 END) / 3.0, 2) AS usd_last_3m_avg,
         ROUND(SUM(CASE WHEN p.usage_date >= b.prior3_start AND p.usage_date < b.last3_start THEN p.usd ELSE 0 END) / 3.0, 2) AS usd_prior_3m_avg,
         ROUND(SUM(CASE WHEN p.usage_date >= b.window_start AND p.usage_date < b.mid3_end THEN p.usd ELSE 0 END) / 3.0, 2) AS usd_first_3m_avg
  FROM priced p, bounds b
  GROUP BY p.workspace_id, p.sku_name
),
sku_metrics AS (
  SELECT sb.workspace_id, sb.sku_name, sb.usd_last_3m_avg, sb.usd_prior_3m_avg, sb.usd_first_3m_avg,
         ROUND((sb.usd_last_3m_avg - sb.usd_prior_3m_avg) / NULLIF(sb.usd_prior_3m_avg, 0) * 100, 1) AS growth_3m_pct,
         ROUND((sb.usd_last_3m_avg - sb.usd_first_3m_avg) / NULLIF(sb.usd_first_3m_avg, 0) * 100, 1) AS growth_12m_pct,
         ROUND(100.0 * sb.usd_last_3m_avg / NULLIF(SUM(sb.usd_last_3m_avg) OVER (PARTITION BY sb.workspace_id), 0), 1) AS share_of_total_last_3m,
         (sb.usd_first_3m_avg = 0) AS new_this_year
  FROM sku_bucket sb
)
SELECT __W__ AS window_days, m.workspace_id, m.sku_name, m.billing_origin_product,
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
       END AS product_label,
       m.month_start,
       (m.month_start = b.cur_month_start) AS is_partial_month,
       m.net_list_cost_usd, m.price_basis,
       sm.usd_last_3m_avg, sm.usd_prior_3m_avg, sm.growth_3m_pct, sm.usd_first_3m_avg, sm.growth_12m_pct,
       sm.share_of_total_last_3m, sm.new_this_year,
       CASE
         WHEN sm.growth_3m_pct >= __P__crit_growth_pct__ AND sm.usd_last_3m_avg >= __P__crit_min_usd__ THEN 'CRITICAL'
         WHEN sm.growth_3m_pct >= __P__warn_growth_pct__ AND sm.usd_last_3m_avg >= __P__warn_min_usd__ THEN 'WARN'
         WHEN sm.new_this_year AND sm.usd_last_3m_avg >= __P__new_sku_min_usd__ THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM monthly m
CROSS JOIN bounds b
JOIN sku_metrics sm ON sm.sku_name = m.sku_name AND sm.workspace_id IS NOT DISTINCT FROM m.workspace_id
