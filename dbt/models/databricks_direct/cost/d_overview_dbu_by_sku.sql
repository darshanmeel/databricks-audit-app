{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/app/cost/overview_dbu_by_sku.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT
    workspace_id,
    sku_name,
    billing_origin_product,
    SUM(usage_quantity) AS net_dbus
FROM {{ source('system_billing', 'usage') }}
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY workspace_id, sku_name, billing_origin_product
ORDER BY net_dbus DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
