{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'stars', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_premium_serverless_photon.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product,
       product_features.is_serverless      AS is_serverless,
       product_features.is_photon          AS is_photon,
       product_features.jobs_tier          AS jobs_tier,
       product_features.sql_tier           AS sql_tier,
       product_features.dlt_tier           AS dlt_tier,
       product_features.performance_target AS performance_target,
       SUM(usage_quantity) AS net_usage_quantity
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product,
         product_features.is_serverless, product_features.is_photon, product_features.jobs_tier,
         product_features.sql_tier, product_features.dlt_tier, product_features.performance_target
ORDER BY usage_date DESC, sku_name, cloud
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
