-- query_id: instance_pools_idle_capacity
-- title: Instance pool idle-capacity floor (min_idle_instances)
-- domain: compute   tier: standard
-- reads: system.compute.instance_pools, system.billing.usage, system.billing.list_prices
-- requires: SELECT on system.compute, system.billing; Public Preview (system.compute.instance_pools may be empty or disabled per workspace)
-- empty_if: preview_unavailable
-- params: :period_days (default 30) rolling window in days for the cost rollup (this is a config snapshot with no time window of its own); :warn_min_idle_instances (default 5) standing idle-instance floor that flags WARN; :crit_min_idle_instances (default 20) standing idle-instance floor that flags CRITICAL
-- confidence: needs_confirmation
-- confidence_note: system.compute.instance_pools is Public Preview and may be empty or disabled in your workspace; the columns themselves are confirmed.
-- read_this: One row = the latest known configuration for one instance pool that has not been deleted, plus its compute spend over :period_days. The column that matters is min_idle_instances - the number of instances this pool keeps warm and idle at all times, which bills regardless of whether anything is using them.
-- healthy: status = OK (min_idle_instances below :warn_min_idle_instances) - field heuristic; tune for your account's pool sizes.
-- investigate_if: status = WARN or CRITICAL (min_idle_instances at or above :warn_min_idle_instances / :crit_min_idle_instances) - field heuristic. Also worth a look regardless of status: idle_instance_autotermination_minutes NULL or 0, which means instances above the floor never get reclaimed.
-- actions: 1) lower min_idle_instances to match actual concurrent start-up demand (free); 2) set or lower idle_instance_autotermination_minutes so instances above the floor get reclaimed instead of idling indefinitely (config); 3) if est_usd_list is high and demand is genuinely bursty, move the workload to Serverless compute instead of a warm pool (spend).
-- next: classic_clusters_config_current (to see which clusters draw from this pool via driver_instance_pool_id/worker_instance_pool_id), node_types_reference (to size the node_type this pool uses)
-- caveats: SCD snapshot: this returns the latest row per instance_pool_id, so a deleted pool (delete_time NOT NULL) is excluded. min_idle_instances/max_capacity are bigint. disk_spec/aws_attributes/azure_attributes/gcp_attributes are STRUCTs selected whole - only the cloud you actually run on has its struct populated, and their subfields are example-documented only, so pulling one specific subfield out is needs_confirmation. True idle-waste dollarization (min_idle_instances x node cost) is not possible from compute tables alone - node-type cost is not billed as its own line item, only rolled-up compute spend is. net_dbus is exact billed DBUs (usage_unit='DBU'); est_usd_list is an ESTIMATE AT THE EFFECTIVE LIST PRICE (usage_quantity x list_prices.pricing.effective_list.default - DEC-66.1) - NOT your negotiated invoice rate (not available in any system table) and excludes cloud infra/egress cost; treat est_usd_list as directional. Cost is attributed by billing ID (usage_metadata.instance_pool_id) over the :period_days window (per-pool), not per event - the cost rollup is pre-aggregated before the join, so rows are never multiplied. This query is a config SNAPSHOT with no time window of its own; the cost columns use the :period_days window as the pool's total compute spend (all instances drawing from the pool), not a measure of idle-only waste. Pools are billed via the clusters that use them, tagged with instance_pool_id on billing.usage. instance_pool_id is a globally-unique GUID; the rollup is keyed on workspace_id + instance_pool_id (both present here) to keep the join strictly 1:1. Regional - run per metastore region. price_basis is 'unpriced' when any non-free-usage SKU billed to this pool had no matching list_prices row (net_dbus/est_usd_list then understate cost), 'free' when every matched SKU is a FREE_USAGE SKU (a real $0), and 'priced' otherwise.
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM system.billing.list_prices
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
  FROM system.billing.usage u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.instance_pool_id IS NOT NULL
    AND u.usage_date >= date_add(current_date(), -CAST(:period_days AS INT))
    AND u.usage_date <  current_date()
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
       -- status: worst-first band on the standing idle-instance floor (field heuristic; :warn_min_idle_instances / :crit_min_idle_instances).
       CASE
         WHEN p.min_idle_instances IS NULL THEN 'NOT_ASSESSED'
         WHEN p.min_idle_instances >= :crit_min_idle_instances THEN 'CRITICAL'
         WHEN p.min_idle_instances >= :warn_min_idle_instances THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
  FROM system.compute.instance_pools
) p
LEFT JOIN cost_rollup cr
  ON cr.workspace_id = p.workspace_id
 AND cr.instance_pool_id = p.instance_pool_id
WHERE p.rn = 1 AND p.delete_time IS NULL
ORDER BY p.min_idle_instances DESC
