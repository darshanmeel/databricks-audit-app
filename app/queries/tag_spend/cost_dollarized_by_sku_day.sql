-- cost_dollarized_by_sku_day over the dollars a tag filter keeps (app/core/tag_spend.py). The
-- spend table carries no usage_type, so it reads NULL here.
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day),
parts AS (
  SELECT t.workspace_id, t.usage_date, t.cloud, t.sku_name, t.billing_origin_product, t.usage_unit,
         'priced' AS price_basis, t.quantity - t.unpriced_quantity AS qty, t.usd AS cost
  FROM tagged t, snap a
  WHERE t.usage_date >= a.d - __W__ AND t.usage_date < a.d AND t.quantity - t.unpriced_quantity <> 0
  UNION ALL
  SELECT t.workspace_id, t.usage_date, t.cloud, t.sku_name, t.billing_origin_product, t.usage_unit,
         CASE WHEN upper(t.sku_name) LIKE '%FREE_USAGE%' THEN 'free' ELSE 'unpriced' END,
         t.unpriced_quantity, CAST(NULL AS DOUBLE)
  FROM tagged t, snap a
  WHERE t.usage_date >= a.d - __W__ AND t.usage_date < a.d AND t.unpriced_quantity <> 0
)
SELECT __W__ AS window_days, workspace_id, usage_date, cloud, sku_name, billing_origin_product,
       CAST(NULL AS VARCHAR) AS usage_type, usage_unit,
       CASE WHEN price_basis = 'priced' THEN 'USD' END AS currency_code,
       SUM(qty) AS net_usage_quantity, SUM(cost) AS net_list_cost, price_basis
FROM parts
GROUP BY workspace_id, usage_date, cloud, sku_name, billing_origin_product, usage_unit, price_basis
