{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/app/cost/overview_daily_dbu_trend.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT
    workspace_id,
    usage_date,
    SUM(usage_quantity) AS net_dbus
FROM {{ source('system_billing', 'usage') }}
WHERE usage_unit = 'DBU'
  AND usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND usage_date < {{ audit_today() }}
GROUP BY workspace_id, usage_date
ORDER BY usage_date
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
