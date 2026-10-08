-- query_id: cost_genai_token_gpu
-- title: GenAI token and GPU usage
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: The usage_type enum (COMPUTE_TIME, STORAGE_SPACE, NETWORK_BYTE, NETWORK_HOUR, API_OPERATION, TOKEN, GPU_TIME, ANSWER) is documented.
-- read_this: One row = a workspace + day + cloud + SKU + usage type's GenAI usage. The columns that matter are usage_type (TOKEN vs GPU_TIME vs ANSWER can be different units) and net_usage_quantity in that unit, priced at usd_list - each usage_type is priced against its OWN list_prices SKU row (never assume the DBU rate; TOKEN rows we've reviewed ARE actually DBU-denominated on some SKUs, so guessing the unit instead of joining on it would misprice them). price_basis (free/priced/unpriced) discloses whether usd_list is a real $0 or a pricing-coverage gap.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_serving_mode_by_endpoint (for the dollarized, per-endpoint MODEL_SERVING cut), cost_vector_search_spend (for the dollarized VECTOR_SEARCH cut)
-- caveats: The confirmed usage_type enum is COMPUTE_TIME, STORAGE_SPACE, NETWORK_BYTE, NETWORK_HOUR, API_OPERATION, TOKEN, GPU_TIME, ANSWER. usage_unit varies by usage_type and is not reliably DBU-only or never-DBU by type - TOKEN rows we reviewed are actually priced in DBU on some SKUs - so usd_list is priced by joining system.billing.list_prices on each row's OWN (sku_name, cloud, usage_unit) rather than assuming a unit; any further rollup should still group by usage_unit, as this query already does. usage_metadata endpoint fields populate only for model-serving / vector-search. endpoint_name is a resource name and is never masked (DEC-73). workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it does not change any other column's meaning.
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
       u.product_features.serving_type AS serving_type,
       u.usage_metadata.endpoint_name  AS endpoint_name,
       u.usage_metadata.endpoint_id    AS endpoint_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
  AND u.usage_type IN ('TOKEN', 'GPU_TIME', 'ANSWER')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_type, u.usage_unit,
         u.product_features.serving_type, u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id
ORDER BY usage_date DESC, cloud, usage_type
