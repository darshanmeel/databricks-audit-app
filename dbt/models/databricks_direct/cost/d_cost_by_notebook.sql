{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_by_notebook.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       usage_metadata.notebook_id     AS notebook_id,
       product_features.is_serverless AS is_serverless,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: magnitude band on daily DBU cost per notebook (field heuristic; {{ param('cost_by_notebook', 'warn_notebook_dbus_per_day', 10) }} / {{ param('cost_by_notebook', 'crit_notebook_dbus_per_day', 50) }}).
       CASE
         WHEN SUM(usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(usage_quantity) >= {{ param('cost_by_notebook', 'crit_notebook_dbus_per_day', 50) }} THEN 'CRITICAL'
         WHEN SUM(usage_quantity) >= {{ param('cost_by_notebook', 'warn_notebook_dbus_per_day', 10) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_billing', 'usage') }}
WHERE usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
  AND usage_unit = 'DBU'
  AND usage_metadata.notebook_id IS NOT NULL
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         usage_metadata.notebook_id, product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
