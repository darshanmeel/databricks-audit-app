-- cost_monthly_actuals over the dollars a tag filter keeps (app/core/tag_spend.py).
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d, min(usage_date) AS snapshot_start FROM tags.cost_day)
SELECT __W__ AS window_days, t.workspace_id, t.billing_origin_product,
       CAST(date_trunc('MONTH', t.usage_date) AS DATE) AS month_start,
       CASE WHEN SUM(t.quantity - t.unpriced_quantity) <> 0 THEN 'USD' END AS currency_code,
       ROUND(SUM(t.usd), 2) AS net_list_cost_usd,
       COUNT(DISTINCT t.usage_date) AS days_captured,
       CASE
         WHEN CAST(date_trunc('MONTH', t.usage_date) AS DATE) = CAST(date_trunc('MONTH', a.d) AS DATE) THEN TRUE
         WHEN a.snapshot_start > CAST(date_trunc('MONTH', t.usage_date) AS DATE) THEN TRUE
         ELSE FALSE
       END AS is_partial_month,
       CASE
         WHEN CAST(date_trunc('MONTH', t.usage_date) AS DATE) = CAST(date_trunc('MONTH', a.d) AS DATE) THEN 'month_in_progress'
         WHEN a.snapshot_start > CAST(date_trunc('MONTH', t.usage_date) AS DATE) THEN 'export_starts_mid_month'
       END AS partial_reason,
       CASE
         WHEN SUM(CASE WHEN upper(t.sku_name) NOT LIKE '%FREE_USAGE%' THEN t.unpriced_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(t.quantity - t.unpriced_quantity) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       MIN(t.usage_date) AS first_day,
       MAX(t.usage_date) AS last_day
FROM tagged t, snap a
WHERE t.usage_date < a.d
GROUP BY t.workspace_id, t.billing_origin_product, CAST(date_trunc('MONTH', t.usage_date) AS DATE),
         a.d, a.snapshot_start
