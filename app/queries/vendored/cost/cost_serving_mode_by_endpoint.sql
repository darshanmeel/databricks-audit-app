-- query_id: cost_serving_mode_by_endpoint
-- title: Model-serving spend by endpoint and inferred cost mode
-- domain: cost   tier: deep
-- reads: system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.billing; GA (system.billing.usage and system.billing.list_prices are generally available)
-- empty_if: no_activity
-- params: :period_days (default 30) rolling window in days; :warn_endpoint_usd_per_day (default 50) estimated list-price $/day on a single workspace + endpoint + day + usage_type that flags WARN; :crit_endpoint_usd_per_day (default 250) $/day that flags CRITICAL
-- confidence: confirmed
-- confidence_note: The billing columns (billing_origin_product, product_features.serving_type, usage_type, usage_metadata.endpoint_name/endpoint_id, the LAUNCH SKU pattern) are all doc-confirmed; pricing.effective_list.default (the effective, post-promotion list price - DEC-66.1) is the one dollar basis used across this app, so net_list_cost is a directional estimate at that price, not a negotiated or billed dollar figure.
-- read_this: One row = a workspace + endpoint + day + usage_type's model-serving usage with an inferred cost mode. The columns that matter are is_launch_sku (a TRUE-heavy endpoint is repeatedly cold-starting from scale-to-zero, which is itself a cost signal) and net_list_cost (est_usd_list, the estimated dollar exposure that drives the PER-DAY band below). price_basis (free/priced/unpriced) discloses whether net_list_cost is a real $0 (free-usage SKU) or understated by a pricing-coverage gap.
-- healthy: net_list_cost below :warn_endpoint_usd_per_day est_usd_list/day per workspace + endpoint + day + usage_type (field heuristic - tune for your account).
-- investigate_if: net_list_cost at/above :warn_endpoint_usd_per_day (WARN) or :crit_endpoint_usd_per_day (CRITICAL) est_usd_list/day on a SINGLE usage_date (field heuristic); or net_list_cost is NULL on a row whose price_basis is unpriced (NOT_ASSESSED - a pricing-coverage gap, not $0). A high is_launch_sku share alongside a high band is the strongest signal of scale-to-zero churn.
-- actions: 1) confirm the endpoint's traffic pattern actually needs to scale from zero this often, or whether a minimum-provisioned-throughput floor would be cheaper (free); 2) switch a steadily-busy endpoint from pay-per-token/scale-to-zero to provisioned throughput, or the reverse for a bursty one (config); 3) right-size the endpoint's provisioned capacity or model choice (spend).
-- next: cost_by_serving_endpoint (for the raw, non-dollarized per-endpoint usage split by usage_type), compute_serving_endpoint_cost_status (to check whether a CRITICAL endpoint is actually seeing real traffic)
-- caveats: All billing columns here are doc-confirmed: billing_origin_product='MODEL_SERVING'; product_features.serving_type enum {MODEL, GPU_MODEL, FOUNDATION_MODEL, FEATURE, null}; usage_type enum includes COMPUTE_TIME/GPU_TIME/TOKEN/ANSWER; usage_metadata.endpoint_name/endpoint_id; the SKU pattern '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%' identifies scale-from-zero cold-start launches per the model-serving-cost monitoring documentation. The cost mode is inferred from these billed signals, not read from a config column - Databricks does not expose workload_type/workload_size/scale_to_zero_enabled in system tables (those are serving-endpoints API fields only). list_rate is pricing.effective_list.default, the effective (post-promotion) list price - DEC-66.1's one basis, used everywhere else in this app; net_list_cost is NULL when the price join found no match, and that degrades to "list cost unavailable," never an invented rate. net_list_cost is an estimate at the effective list price (est_usd_list), never the negotiated/invoice rate (no negotiated-rate source exists, see cost_actual_vs_list_by_sku). Empty result means model serving is not in use in the window, a real result, not $0. usage_date is now in the SELECT and GROUP BY, matching read_this's "workspace + endpoint + day + usage_type" grain and the :warn_endpoint_usd_per_day / :crit_endpoint_usd_per_day param names, which are both PER-DAY thresholds -- an earlier version of this query grouped by endpoint + usage_type only and compared that whole-window total against the per-day threshold, which overstated the daily rate by roughly the window length, so an endpoint spending only about 1/7 (7-day window) to 1/90 (90-day window) of the per-day threshold per day was flagged WARN/CRITICAL (false flags). See cost_vector_search_spend, which never had this bug, for the same per-day shape. The price join is keyed on sku_name + cloud + usage_unit, same as cost_dollarized_by_sku_day, so a SKU priced in two units cannot fan out. price_basis is 'unpriced' when this endpoint/day/usage_type/SKU row had no matching list_prices row and is not a FREE_USAGE SKU (net_list_cost is then NULL, a coverage gap, never $0), 'free' when the SKU is a FREE_USAGE SKU with no price row by design (a real $0), and 'priced' otherwise. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it never changes the endpoint + day + usage_type grain, since one endpoint bills to one workspace.
-- This scopes to billing_origin_product='MODEL_SERVING', so Vector Search endpoint spend (billed separately under VECTOR_SEARCH) is not counted here; feature/function and agent endpoints do bill under MODEL_SERVING and are included. See cost_vector_search_spend for vector-search cost.
SELECT
  u.usage_date                           AS usage_date,
  u.workspace_id                         AS workspace_id,
  u.usage_metadata.endpoint_id           AS endpoint_id,
  u.usage_metadata.endpoint_name AS endpoint_name,
  u.cloud                                AS cloud,
  u.sku_name                             AS sku_name,
  u.usage_type                           AS usage_type,
  u.usage_unit                           AS usage_unit,
  u.product_features.serving_type        AS serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%'
       THEN TRUE ELSE FALSE END          AS is_launch_sku,
  SUM(u.usage_quantity)                  AS net_usage_quantity,
  SUM(u.usage_quantity * lp.list_rate)   AS net_list_cost,
  -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (NULL, understates cost).
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END                                     AS price_basis,
  -- status: est_usd_list/day band per endpoint + day + usage_type (field heuristic; :warn_endpoint_usd_per_day / :crit_endpoint_usd_per_day).
  -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
  -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
  CASE
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
    WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= :crit_endpoint_usd_per_day THEN 'CRITICAL'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= :warn_endpoint_usd_per_day THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM system.billing.list_prices
) lp
  ON  u.sku_name = lp.sku_name
  AND u.cloud    = lp.cloud
  AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date <  current_date()
  AND u.billing_origin_product = 'MODEL_SERVING'
GROUP BY
  u.usage_date,
  u.workspace_id,
  u.usage_metadata.endpoint_id,
  u.usage_metadata.endpoint_name,
  u.cloud,
  u.sku_name,
  u.usage_type,
  u.usage_unit,
  u.product_features.serving_type,
  CASE WHEN upper(u.sku_name) LIKE '%SERVERLESS_REAL_TIME_INFERENCE_LAUNCH%' THEN TRUE ELSE FALSE END,
  CASE
    WHEN lp.list_rate IS NOT NULL THEN 'priced'
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' THEN 'free'
    ELSE 'unpriced'
  END
ORDER BY net_list_cost DESC NULLS LAST
