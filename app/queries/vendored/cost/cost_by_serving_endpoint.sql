-- query_id: cost_by_serving_endpoint
-- title: Usage by model-serving / vector-search endpoint
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: usage_metadata.endpoint_id/endpoint_name are documented for MODEL_SERVING; same paths as cost_serving_mode_by_endpoint.
-- read_this: One row = a day + workspace + endpoint's serving/vector-search usage. The columns that matter are endpoint_id, usage_type (COMPUTE_TIME / GPU_TIME / TOKEN are different units), and net_usage_quantity in that unit - this is the size map behind the model-serving and vector-search share of spend, not a right-sizing verdict on its own.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_serving_mode_by_endpoint (for a dollarized, cost-mode-aware cut of the same MODEL_SERVING usage), cost_vector_search_spend (for the dollarized VECTOR_SEARCH cut)
-- caveats: This sizes the spend per endpoint - the right-sizing config (scale-to-zero, provisioned vs used throughput) is not in any system table (serving-endpoints API only), so this is the "where the spend goes" map, not the fix itself. usage_quantity is labeled DBU here but usage_type spans COMPUTE_TIME / GPU_TIME / TOKEN, which are different physical units - never sum across usage_type. endpoint_name is partial-masked in-SQL (first 2 chars + ****). Empty result means serving/vector search is not in use in the window, a real result, not $0.
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.endpoint_id AS endpoint_id,
       usage_metadata.endpoint_name AS endpoint_name,
       usage_type,
       SUM(usage_quantity) AS net_usage_quantity
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
  AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH')
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.endpoint_id, usage_metadata.endpoint_name, usage_type
ORDER BY usage_date DESC, workspace_id, endpoint_id
