{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_by_billing_origin_product.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT billing_origin_product, usage_unit, cloud, workspace_id,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*)            AS record_count
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY billing_origin_product, usage_unit, cloud, workspace_id
ORDER BY billing_origin_product, usage_unit, cloud, workspace_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
