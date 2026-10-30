-- cost_by_billing_origin_product over the dollars a tag filter keeps (app/core/tag_spend.py).
-- not reproduced: record_count
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day)
SELECT __W__ AS window_days, t.billing_origin_product, t.usage_unit, t.cloud, t.workspace_id,
       SUM(t.quantity) AS net_usage_quantity,
       CAST(NULL AS BIGINT) AS record_count
FROM tagged t, snap a
WHERE t.usage_date >= a.d - __W__ AND t.usage_date < a.d
GROUP BY t.billing_origin_product, t.usage_unit, t.cloud, t.workspace_id
