{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_by_serving_endpoint.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.endpoint_id AS endpoint_id,
       usage_metadata.endpoint_name AS endpoint_name,
       usage_type,
       SUM(usage_quantity) AS net_usage_quantity
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
  AND usage_unit = 'DBU'
  AND billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH')
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.endpoint_id, usage_metadata.endpoint_name, usage_type
ORDER BY usage_date DESC, workspace_id, endpoint_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
