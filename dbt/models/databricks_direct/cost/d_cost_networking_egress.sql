{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_networking_egress.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
       u.usage_metadata.source_region      AS source_region,
       u.usage_metadata.destination_region AS destination_region,
       u.usage_metadata.networking_client  AS networking_client,
       u.usage_metadata.recipient_id       AS recipient_id,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(p.list_rate, 0)), 2) AS usd_list,
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
) p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date < {{ audit_today() }}
  AND u.usage_type IN ('NETWORK_BYTE', 'NETWORK_HOUR')
GROUP BY u.usage_date, u.cloud, u.workspace_id, u.sku_name, u.usage_type, u.usage_unit,
         u.usage_metadata.source_region, u.usage_metadata.destination_region,
         u.usage_metadata.networking_client, u.usage_metadata.recipient_id
ORDER BY usage_date DESC, cloud, usage_type
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
