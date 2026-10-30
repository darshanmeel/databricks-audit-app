{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:deep', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_actual_vs_list_by_sku.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product,
       SUM(u.usage_quantity)                       AS net_usage_quantity,
       SUM(u.usage_quantity * lp.list_rate)        AS net_list_cost,
       -- status: this check can never be assessed - no negotiated-rate source exists anywhere in
       -- system.billing, so every row reads NOT_ASSESSED instead of a fabricated realization band.
       'NOT_ASSESSED'                                                      AS status,
       'no_negotiated_rate_source'                                        AS not_assessed_reason
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate   -- effective (post-promotion) list price - DEC-66.1's basis
  FROM {{ list_prices() }} list_prices
) lp
  ON  u.sku_name      = lp.sku_name
  AND u.cloud         = lp.cloud
  AND u.usage_unit    = lp.usage_unit
  AND u.usage_date    >= DATE(lp.price_start_time)
  AND (lp.price_end_time IS NULL OR u.usage_date < DATE(lp.price_end_time))   -- active rows carry NULL end_time
WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date < {{ audit_today() }}
  AND upper(u.usage_unit) = 'DBU'   -- price only DBU rows against a per-DBU rate; never blend bytes/hours/tokens
GROUP BY u.workspace_id, u.cloud, u.sku_name, u.usage_unit, u.billing_origin_product
ORDER BY net_list_cost DESC NULLS LAST
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
