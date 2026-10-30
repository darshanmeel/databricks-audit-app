{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_totals_by_sku_day.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN usage_quantity ELSE 0 END) AS retraction_usage_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity ELSE 0 END) AS restatement_usage_quantity,
       COUNT(*) AS record_count
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product, usage_type, usage_unit,
         product_features.is_serverless
ORDER BY usage_date DESC, workspace_id, sku_name
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
