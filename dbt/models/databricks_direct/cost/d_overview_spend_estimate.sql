{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['inventory', 'domain:cost', 'tier:lite', 'databricks_direct']) }}
-- generated from app/queries/app/cost/overview_spend_estimate.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT
    workspace_id,
    SUM(u.usage_quantity) AS total_net_dbus,
    'USD' AS currency_code,
    ROUND(SUM(u.usage_quantity * lp.list_rate), 2) AS net_list_cost_usd,
    'list' AS dbu_price_source,
    -- price_basis: distinguishes a real $0 (free-usage SKUs, no list_prices row by design) from
    -- a coverage gap (a priced SKU with no matching list_prices row, which understates the total).
    CASE
      WHEN SUM(CASE WHEN lp.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                    THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
      WHEN SUM(CASE WHEN lp.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
      ELSE 'priced'
    END AS price_basis
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, currency_code, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
) lp
  ON u.sku_name = lp.sku_name
 AND u.cloud = lp.cloud
 AND u.usage_unit = lp.usage_unit
 AND u.usage_end_time >= lp.price_start_time
 AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
WHERE u.usage_unit = 'DBU'
  AND u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date < {{ audit_today() }}
GROUP BY workspace_id
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
