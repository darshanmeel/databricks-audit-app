{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_cloud_infra.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT u.workspace_id, u.usage_date, u.cloud, p.currency_code,
       -- bare SUM (no COALESCE): a fully-unpriced day/cloud/currency group must stay NULL, never a
       -- silent $0 -- see caveats ("An empty result means not assessed, never $0").
       SUM(u.usage_quantity * p.list_rate)               AS net_list_cost,
       COUNT(*)                                         AS record_count,
       -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
       -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
       CASE
         WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END                                               AS price_basis
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, currency_code, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
) p
  ON u.sku_name = p.sku_name
 AND u.cloud = p.cloud
 AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date < {{ audit_today() }}
GROUP BY u.workspace_id, u.usage_date, u.cloud, p.currency_code
ORDER BY u.usage_date DESC, u.cloud, p.currency_code
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
