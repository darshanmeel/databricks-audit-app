-- query_id: cost_by_compute_resource
-- title: DBU cost by cluster, warehouse, and pool
-- domain: cost   tier: standard
-- reads: system.billing.usage, system.billing.list_prices, system.compute.clusters, system.compute.warehouses, system.compute.instance_pools, system.access.workspaces_latest
-- requires: SELECT on system.billing, system.compute, system.access; billing usage/list_prices and system.compute.{clusters, warehouses, instance_pools} are GA, workspaces_latest is Public Preview
-- params: :period_days (default 30) rolling window in days; :warn_resource_dbus_per_day (default 50) DBUs/day on a single cluster, warehouse, or instance pool that flags WARN; :crit_resource_dbus_per_day (default 200) DBUs/day that flags CRITICAL
-- confidence: confirmed
-- confidence_note: usage_metadata.{cluster_id, warehouse_id, instance_pool_id} and product_features.is_serverless are documented system.billing.usage columns.
-- read_this: One row = a day + workspace + compute resource's DBU cost. The columns that matter are cluster_id/warehouse_id/instance_pool_id (which resource, resolved to a name alongside it) and net_usage_quantity (its DBU burn for that day, priced at usd_list) - a resource that stays above the WARN/CRITICAL band day after day is the one worth right-sizing or decommissioning first. price_basis (free/priced/unpriced) discloses whether usd_list is a real $0 (free-usage SKU) or understated by a pricing-coverage gap. status is a SPEND-MAGNITUDE rank, not a waste signal: cross-check compute_idle_node_ratio (clusters) or compute_warehouse_idle_minutes (warehouses) before treating a flagged row as wasteful rather than simply large.
-- healthy: net_usage_quantity below :warn_resource_dbus_per_day DBUs/day per resource (field heuristic - tune :warn_resource_dbus_per_day for your account).
-- investigate_if: net_usage_quantity at/above :warn_resource_dbus_per_day (WARN) or :crit_resource_dbus_per_day (CRITICAL) DBUs/day - field heuristic; a single spike day matters less than the same resource showing up repeatedly. A CRITICAL/WARN row means "large", not "wasteful".
-- actions: 1) confirm the cluster/warehouse/pool is still needed and not an orphaned always-on resource (free); 2) resolve the resource's name via classic_clusters_config_current or sql_warehouse_config_current and right-size its node type or auto-stop/auto-scale settings (config); 3) move steady-state heavy workloads to a reserved/committed-use tier (spend).
-- next: classic_clusters_config_current (to resolve cluster_id to a name and config), sql_warehouse_config_current (to resolve warehouse_id to a name and config)
-- caveats: usage_metadata.{cluster_id, warehouse_id, instance_pool_id} populate for the compute that generated the usage; they are NULL for serverless / account-level lines - those roll up under NULL and are kept, not dropped. cluster_name/warehouse_name/instance_pool_name resolve from each resource's latest (by change_time) system.compute.* row and may be null if the resource has since been deleted or its name was never captured; join cluster_id -> classic_clusters_config_current and warehouse_id -> sql_warehouse_config_current for full config detail. usage_quantity is DBU; usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1), not your negotiated invoice rate, priced the same way every other cost_* query in this set prices. price_basis is 'unpriced' when any non-free-usage SKU billed to this resource/day had no matching list_prices row (usd_list then understates cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise. Net corrections: this sums usage_quantity across all record_types. workspace_name comes from the Public Preview workspaces_latest table and may be null.
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
       -- status: magnitude band on daily DBU cost per resource (field heuristic; :warn_resource_dbus_per_day / :crit_resource_dbus_per_day) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity) >= :crit_resource_dbus_per_day THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity) >= :warn_resource_dbus_per_day THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
) pr
  ON  u.sku_name = pr.sku_name AND u.cloud = pr.cloud AND u.usage_unit = pr.usage_unit
  AND u.usage_end_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_end_time < pr.price_end_time)
LEFT JOIN (
  SELECT workspace_id, cluster_id, cluster_name
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
) lc
  ON lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
LEFT JOIN (
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
) lw
  ON lw.workspace_id = u.workspace_id AND lw.warehouse_id = u.usage_metadata.warehouse_id
LEFT JOIN (
  SELECT workspace_id, instance_pool_id, instance_pool_name
  FROM system.compute.instance_pools
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) = 1
) lp
  ON lp.workspace_id = u.workspace_id AND lp.instance_pool_id = u.usage_metadata.instance_pool_id
LEFT JOIN system.access.workspaces_latest w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -:period_days, current_date())
  AND u.usage_date < current_date()
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
