-- cost_premium_serverless_photon over the dollars a tag filter keeps (app/core/tag_spend.py).
-- not reproduced: jobs_tier, sql_tier, dlt_tier, performance_target
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day)
SELECT __W__ AS window_days, t.usage_date, t.cloud, t.workspace_id, t.sku_name, t.billing_origin_product,
       t.is_serverless, t.is_photon,
       CAST(NULL AS VARCHAR) AS jobs_tier, CAST(NULL AS VARCHAR) AS sql_tier,
       CAST(NULL AS VARCHAR) AS dlt_tier, CAST(NULL AS VARCHAR) AS performance_target,
       SUM(t.quantity) AS net_usage_quantity
FROM tagged t, snap a
WHERE t.usage_unit = 'DBU' AND t.usage_date >= a.d - __W__ AND t.usage_date < a.d
GROUP BY t.usage_date, t.cloud, t.workspace_id, t.sku_name, t.billing_origin_product, t.is_serverless, t.is_photon
