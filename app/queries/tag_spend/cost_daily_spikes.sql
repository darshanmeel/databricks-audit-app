-- cost_daily_spikes over the dollars a tag filter keeps (app/core/tag_spend.py); same baseline,
-- thresholds and drivers as the check itself.
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day),
daily_sku AS (
  SELECT t.workspace_id, t.billing_origin_product, t.usage_date, t.sku_name,
         SUM(t.usd) AS sku_cost,
         SUM(t.quantity) AS sku_quantity,
         bool_and(t.usd IS NULL) AS all_unpriced
  FROM tagged t, snap a
  WHERE t.usage_date >= a.d - (__W__ + __P__baseline_days__) AND t.usage_date < a.d
  GROUP BY t.workspace_id, t.billing_origin_product, t.usage_date, t.sku_name
),
daily AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         SUM(sku_cost) AS day_cost,
         SUM(CASE WHEN all_unpriced AND upper(sku_name) NOT LIKE '%FREE_USAGE%' THEN sku_quantity ELSE 0 END) AS unpriced_quantity,
         SUM(CASE WHEN NOT all_unpriced THEN sku_quantity ELSE 0 END) AS priced_quantity
  FROM daily_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
),
candidates AS (
  SELECT dd.* FROM daily dd, snap a WHERE dd.usage_date >= a.d - __W__
),
baseline AS (
  SELECT c.workspace_id, c.billing_origin_product, c.usage_date AS candidate_date,
         quantile_cont(b.day_cost, 0.5) AS baseline_median_cost, COUNT(*) AS baseline_days_seen
  FROM candidates c
  JOIN daily b
    ON  b.workspace_id IS NOT DISTINCT FROM c.workspace_id
    AND b.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
    AND b.usage_date >= c.usage_date - __P__baseline_days__
    AND b.usage_date < c.usage_date
  GROUP BY c.workspace_id, c.billing_origin_product, c.usage_date
),
ranked_sku AS (
  SELECT s.workspace_id, s.billing_origin_product, s.usage_date, s.sku_name, s.sku_cost
  FROM daily_sku s, snap a
  WHERE s.usage_date >= a.d - __W__
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.workspace_id, s.billing_origin_product, s.usage_date ORDER BY s.sku_cost DESC, s.sku_name
  ) <= 3
),
drivers AS (
  SELECT workspace_id, billing_origin_product, usage_date,
         array_to_string(list_distinct(list(CASE
             WHEN sku_cost IS NULL THEN concat(sku_name,
               CASE WHEN upper(sku_name) LIKE '%FREE_USAGE%' THEN ' (free)' ELSE ' (no list price)' END)
             ELSE concat(sku_name, ' ($', CAST(CAST(ROUND(sku_cost, 0) AS BIGINT) AS VARCHAR), ')')
           END)), ', ') AS top_skus
  FROM ranked_sku
  GROUP BY workspace_id, billing_origin_product, usage_date
)
SELECT __W__ AS window_days, c.workspace_id, c.billing_origin_product, c.usage_date,
       ROUND(c.day_cost, 2) AS est_day_usd_list,
       ROUND(bl.baseline_median_cost, 2) AS est_baseline_usd_list,
       COALESCE(bl.baseline_days_seen, 0) AS baseline_days_seen,
       ROUND(c.day_cost / NULLIF(bl.baseline_median_cost, 0), 2) AS spike_ratio_actual,
       d.top_skus,
       CASE WHEN c.unpriced_quantity > 0 THEN 'unpriced' WHEN c.priced_quantity = 0 THEN 'free' ELSE 'priced' END AS price_basis,
       CASE
         WHEN COALESCE(c.day_cost, 0) < __P__min_spend_usd__ THEN 'OK'
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < __P__min_baseline_days__ THEN 'NOT_ASSESSED'
         WHEN c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * __P__spike_ratio__ THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN COALESCE(c.day_cost, 0) < __P__min_spend_usd__ THEN NULL
         WHEN bl.baseline_days_seen IS NULL OR bl.baseline_days_seen = 0 THEN 'no_baseline_history'
         WHEN bl.baseline_days_seen < __P__min_baseline_days__ THEN 'short_baseline'
       END AS not_assessed_reason
FROM candidates c
LEFT JOIN baseline bl
  ON  bl.workspace_id IS NOT DISTINCT FROM c.workspace_id
  AND bl.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND bl.candidate_date = c.usage_date
LEFT JOIN drivers d
  ON  d.workspace_id IS NOT DISTINCT FROM c.workspace_id
  AND d.billing_origin_product IS NOT DISTINCT FROM c.billing_origin_product
  AND d.usage_date = c.usage_date
WHERE c.day_cost >= __P__min_spend_usd__
  AND ((bl.baseline_days_seen IS NULL OR bl.baseline_days_seen < __P__min_baseline_days__)
       OR c.day_cost >= COALESCE(bl.baseline_median_cost, 0) * __P__spike_ratio__)
