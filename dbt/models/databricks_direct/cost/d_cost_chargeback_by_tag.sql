{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'stars', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_chargeback_by_tag.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*) AS record_count
FROM {{ source('system_billing', 'usage') }}
     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value
ORDER BY usage_date DESC, workspace_id, tag_key, tag_value
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
