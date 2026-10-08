{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_default_storage_dsu.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
       usage_metadata.storage_api_type AS storage_api_type,
       usage_metadata.catalog_id       AS catalog_id,
       SUM(usage_quantity) AS net_usage_quantity
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
  AND usage_metadata.storage_api_type IS NOT NULL   -- confirmed default-storage signal (safer fallback)
GROUP BY usage_date, cloud, workspace_id, sku_name, usage_type, usage_unit,
         usage_metadata.storage_api_type, usage_metadata.catalog_id
ORDER BY usage_date DESC, cloud, sku_name
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
