{{ config(enabled=(target.type == "databricks"), materialized=("table" if var("direct_mode", "run") == "table" else "direct_check"), schema="audit_direct", tags=['finding', 'domain:compute', 'tier:standard', 'databricks_direct']) }}
-- generated from app/queries/vendored/compute/instance_pools_idle_capacity.sql; fix the source query and regenerate, never edit this file.
{%- set windows = var('windows', [7, 30, 90]) %}
{%- for w in windows %}
SELECT {{ w }} AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM {{ list_prices() }} list_prices
),
cost_rollup AS (
  SELECT u.workspace_id,
         u.usage_metadata.instance_pool_id AS instance_pool_id,
         SUM(u.usage_quantity)                            AS net_dbus,
         SUM(u.usage_quantity * COALESCE(p.list_rate, 0)) AS est_usd_list,
         CASE
           WHEN SUM(CASE WHEN p.list_rate IS NULL AND upper(u.sku_name) NOT LIKE '%FREE_USAGE%'
                         THEN u.usage_quantity ELSE 0 END) > 0 THEN 'unpriced'
           WHEN SUM(CASE WHEN p.list_rate IS NOT NULL THEN u.usage_quantity ELSE 0 END) = 0 THEN 'free'
           ELSE 'priced'
         END                                               AS price_basis
  FROM {{ source('system_billing', 'usage') }} u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.instance_pool_id IS NOT NULL
    AND u.usage_date >= date_add({{ audit_today() }}, -CAST({{ w }} AS INT))
    AND u.usage_date <  {{ audit_today() }}
  GROUP BY u.workspace_id, u.usage_metadata.instance_pool_id
)
SELECT p.instance_pool_id,
       p.instance_pool_name AS instance_pool_name,
       p.node_type, p.min_idle_instances, p.max_capacity,
       p.idle_instance_autotermination_minutes, p.enable_elastic_disk, p.preloaded_spark_version,
       p.preloaded_docker_images, p.tags, p.aws_attributes, p.azure_attributes, p.gcp_attributes, p.disk_spec,
       p.create_time, p.delete_time, p.change_time, p.workspace_id, p.account_id,
       COALESCE(cr.net_dbus, 0)     AS net_dbus,
       COALESCE(cr.est_usd_list, 0) AS est_usd_list,
       COALESCE(cr.price_basis, 'priced') AS price_basis,
       -- status: worst-first band on the standing idle-instance floor (field heuristic; {{ param('instance_pools_idle_capacity', 'warn_min_idle_instances', 5) }} / {{ param('instance_pools_idle_capacity', 'crit_min_idle_instances', 20) }}).
       CASE
         WHEN p.min_idle_instances IS NULL THEN 'NOT_ASSESSED'
         WHEN p.min_idle_instances >= {{ param('instance_pools_idle_capacity', 'crit_min_idle_instances', 20) }} THEN 'CRITICAL'
         WHEN p.min_idle_instances >= {{ param('instance_pools_idle_capacity', 'warn_min_idle_instances', 5) }} THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
  FROM {{ source('system_compute', 'instance_pools') }}
) p
LEFT JOIN cost_rollup cr
  ON cr.workspace_id = p.workspace_id
 AND cr.instance_pool_id = p.instance_pool_id
WHERE p.rn = 1 AND p.delete_time IS NULL
ORDER BY p.min_idle_instances DESC
) q
{%- if not loop.last %}
UNION ALL
{%- endif %}
{%- endfor %}
