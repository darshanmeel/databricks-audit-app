{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_serving_mode_by_endpoint.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
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
  -- status: est_usd_list/day band per endpoint + day + usage_type (field heuristic; {{ param('cost_serving_mode_by_endpoint', 'warn_endpoint_usd_per_day', 50) }} / {{ param('cost_serving_mode_by_endpoint', 'crit_endpoint_usd_per_day', 250) }}).
  -- a FREE_USAGE SKU with no matching price row is a real $0 (OK), never NOT_ASSESSED -- that
  -- reading is reserved for a genuine pricing-coverage gap (price_basis = 'unpriced').
  CASE
    WHEN upper(u.sku_name) LIKE '%FREE_USAGE%' AND SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'OK'
    WHEN SUM(u.usage_quantity * lp.list_rate) IS NULL THEN 'NOT_ASSESSED'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= {{ param('cost_serving_mode_by_endpoint', 'crit_endpoint_usd_per_day', 250) }} THEN 'CRITICAL'
    WHEN SUM(u.usage_quantity * lp.list_rate) >= {{ param('cost_serving_mode_by_endpoint', 'warn_endpoint_usd_per_day', 50) }} THEN 'WARN'
    ELSE 'OK'
  END AS status
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1
  FROM {{ list_prices() }} list_prices
) lp
  ON  u.sku_name = lp.sku_name
  AND u.cloud    = lp.cloud
  AND u.usage_unit = lp.usage_unit
  AND u.usage_end_time >= lp.price_start_time
  AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date <  {{ audit_today() }}
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
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
