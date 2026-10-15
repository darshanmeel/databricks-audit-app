-- overview_spend_estimate over the dollars a tag filter keeps (app/core/tag_spend.py).
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day)
SELECT __W__ AS window_days, t.workspace_id,
       SUM(t.quantity) AS total_net_dbus,
       'USD' AS currency_code,
       ROUND(SUM(t.usd), 2) AS net_list_cost_usd,
       'list' AS dbu_price_source,
       CASE
         WHEN SUM(CASE WHEN upper(t.sku_name) NOT LIKE '%FREE_USAGE%' THEN t.unpriced_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(t.quantity - t.unpriced_quantity) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM tagged t, snap a
WHERE t.usage_unit = 'DBU' AND t.usage_date >= a.d - __W__ AND t.usage_date < a.d
GROUP BY t.workspace_id
