-- cost_chargeback_by_workspace over the dollars a tag filter keeps (app/core/tag_spend.py); the
-- share is of the filtered total.
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d, min(usage_date) AS snapshot_start FROM tags.cost_day),
raw_agg AS (
  SELECT COALESCE(t.workspace_id, '__account_level__') AS ws_key, MAX(t.workspace_id) AS workspace_id,
         SUM(CASE WHEN t.usage_date >= a.d - __W__ THEN t.usd END) AS current_cost,
         SUM(CASE WHEN t.usage_date <  a.d - __W__ THEN t.usd END) AS previous_cost,
         SUM(CASE WHEN t.usage_date >= a.d - __W__ AND upper(t.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN t.unpriced_quantity ELSE 0 END) AS current_unpriced_quantity,
         SUM(CASE WHEN t.usage_date >= a.d - __W__ THEN t.quantity - t.unpriced_quantity ELSE 0 END) AS current_priced_quantity,
         SUM(CASE WHEN t.usage_date <  a.d - __W__ AND upper(t.sku_name) NOT LIKE '%FREE_USAGE%'
                  THEN t.unpriced_quantity ELSE 0 END) AS previous_unpriced_quantity,
         SUM(CASE WHEN t.usage_date <  a.d - __W__ THEN t.quantity - t.unpriced_quantity ELSE 0 END) AS previous_priced_quantity
  FROM tagged t, snap a
  WHERE t.usage_date >= a.d - 2 * __W__ AND t.usage_date < a.d
  GROUP BY COALESCE(t.workspace_id, '__account_level__')
),
agg AS (
  SELECT *,
         CASE WHEN current_cost IS NULL AND current_unpriced_quantity = 0 THEN 0 ELSE current_cost END AS eff_current_cost,
         CASE WHEN previous_cost IS NULL AND previous_unpriced_quantity = 0 THEN 0 ELSE previous_cost END AS eff_previous_cost
  FROM raw_agg
),
total AS (SELECT SUM(eff_current_cost) AS total_usd_list FROM agg)
SELECT __W__ AS window_days, g.workspace_id,
       ROUND(g.eff_current_cost, 2) AS usd_list,
       ROUND(g.eff_current_cost * 100.0 / NULLIF(tt.total_usd_list, 0), 1) AS share_of_total_pct,
       ROUND(g.eff_previous_cost, 2) AS prev_usd_list,
       ROUND(g.eff_current_cost - g.eff_previous_cost, 2) AS change_usd_list,
       ROUND((g.eff_current_cost - g.eff_previous_cost) / NULLIF(g.eff_previous_cost, 0) * 100, 1) AS change_pct,
       CASE
         WHEN (g.current_unpriced_quantity + g.previous_unpriced_quantity) > 0 THEN 'unpriced'
         WHEN (g.current_priced_quantity + g.previous_priced_quantity) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       CASE
         WHEN a.snapshot_start > a.d - 2 * __W__ THEN 'NOT_ASSESSED'
         WHEN g.current_cost IS NULL AND g.current_unpriced_quantity > 0 THEN 'NOT_ASSESSED'
         WHEN g.previous_cost IS NULL AND g.previous_unpriced_quantity > 0 THEN 'NOT_ASSESSED'
         WHEN COALESCE(g.eff_current_cost, 0) < __P__min_spend_usd__ THEN 'OK'
         WHEN g.eff_previous_cost = 0 THEN 'CRITICAL'
         WHEN (g.eff_current_cost - g.eff_previous_cost) / NULLIF(g.eff_previous_cost, 0) * 100 >= __P__crit_increase_pct__ THEN 'CRITICAL'
         WHEN (g.eff_current_cost - g.eff_previous_cost) / NULLIF(g.eff_previous_cost, 0) * 100 >= __P__warn_increase_pct__ THEN 'WARN'
         ELSE 'OK'
       END AS status,
       CASE
         WHEN a.snapshot_start > a.d - 2 * __W__ THEN 'previous_window_not_covered'
         WHEN g.current_cost IS NULL AND g.current_unpriced_quantity > 0 THEN 'current_period_unpriced'
         WHEN g.previous_cost IS NULL AND g.previous_unpriced_quantity > 0 THEN 'previous_period_unpriced'
       END AS not_assessed_reason
FROM agg g, snap a, total tt
