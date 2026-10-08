{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:cost', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/cost/cost_by_compute_resource.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
SELECT u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
       u.usage_metadata.cluster_id       AS cluster_id,
       lc.cluster_name,
       u.usage_metadata.warehouse_id     AS warehouse_id,
       lw.warehouse_name,
       u.usage_metadata.instance_pool_id AS instance_pool_id,
       lp.instance_pool_name,
       u.product_features.is_serverless  AS is_serverless,
       SUM(u.usage_quantity) AS net_usage_quantity,
       ROUND(SUM(u.usage_quantity * COALESCE(pr.list_rate, 0)), 2) AS usd_list,
       -- price_basis: a real $0 (free-usage SKU) vs a pricing-coverage gap (usd_list understates cost).
       CASE
         WHEN SUM(CASE WHEN pr.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                       THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
         WHEN SUM(CASE WHEN pr.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
         ELSE 'priced'
       END AS price_basis,
       -- status: magnitude band on daily DBU cost per resource (field heuristic; {{ param('cost_by_compute_resource', 'warn_resource_dbus_per_day', 50) }} / {{ param('cost_by_compute_resource', 'crit_resource_dbus_per_day', 200) }}) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity) >= {{ param('cost_by_compute_resource', 'crit_resource_dbus_per_day', 200) }} THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity) >= {{ param('cost_by_compute_resource', 'warn_resource_dbus_per_day', 50) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM {{ source('system_billing', 'usage') }} u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
) pr
  ON  u.sku_name = pr.sku_name AND u.cloud = pr.cloud AND u.usage_unit = pr.usage_unit
  AND u.usage_end_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_end_time < pr.price_end_time)
LEFT JOIN (
  SELECT workspace_id, cluster_id, cluster_name
  FROM {{ source('system_compute', 'clusters') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
) lc
  ON lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
LEFT JOIN (
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM {{ source('system_compute', 'warehouses') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
) lw
  ON lw.workspace_id = u.workspace_id AND lw.warehouse_id = u.usage_metadata.warehouse_id
LEFT JOIN (
  SELECT workspace_id, instance_pool_id, instance_pool_name
  FROM {{ source('system_compute', 'instance_pools') }}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) = 1
) lp
  ON lp.workspace_id = u.workspace_id AND lp.instance_pool_id = u.usage_metadata.instance_pool_id
LEFT JOIN {{ source('system_access', 'workspaces_latest') }} w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -{{ w }}, {{ audit_today() }})
  AND u.usage_date < {{ audit_today() }}
  AND u.usage_unit = 'DBU'
  AND (u.usage_metadata.cluster_id IS NOT NULL
       OR u.usage_metadata.warehouse_id IS NOT NULL
       OR u.usage_metadata.instance_pool_id IS NOT NULL)
GROUP BY u.usage_date, u.cloud, u.workspace_id, w.workspace_name, u.billing_origin_product,
         u.usage_metadata.cluster_id, lc.cluster_name,
         u.usage_metadata.warehouse_id, lw.warehouse_name,
         u.usage_metadata.instance_pool_id, lp.instance_pool_name,
         u.product_features.is_serverless
ORDER BY net_usage_quantity DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
