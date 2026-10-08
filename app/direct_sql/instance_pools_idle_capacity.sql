-- generated from dbt/models/databricks_direct/compute/d_instance_pools_idle_capacity.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/compute/instance_pools_idle_capacity.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH price AS (
  SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
         CAST(pricing.effective_list.default AS DOUBLE) AS list_rate
  FROM 
(
    SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
           price_start_time, effective_end AS price_end_time
    FROM (
        SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
               price_start_time, price_end_time, next_start_time,
               -- CASE, not LEAST, so a NULL end/next behaves the same on DuckDB and Databricks.
               CASE
                   WHEN price_end_time IS NULL THEN next_start_time
                   WHEN next_start_time IS NULL THEN price_end_time
                   WHEN price_end_time <= next_start_time THEN price_end_time
                   ELSE next_start_time
               END AS effective_end
        FROM (
            SELECT account_id, sku_name, cloud, currency_code, usage_unit, pricing,
                   price_start_time, price_end_time,
                   LEAD(price_start_time) OVER (
                       PARTITION BY sku_name, cloud, usage_unit
                       ORDER BY price_start_time, price_end_time NULLS LAST
                   ) AS next_start_time
            FROM `system`.`billing`.`list_prices`
            WHERE currency_code = 'USD'
        ) ranked
    ) capped
    WHERE effective_end IS NULL OR effective_end > price_start_time
)
 list_prices
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
  FROM `system`.`billing`.`usage` u
  LEFT JOIN price p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE upper(u.usage_unit) = 'DBU'
    AND u.usage_metadata.instance_pool_id IS NOT NULL
    AND u.usage_date >= date_add(__AS_OF_DATE__, -CAST(__WINDOW_DAYS__ AS INT))
    AND u.usage_date <  __AS_OF_DATE__
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
       -- status: worst-first band on the standing idle-instance floor (field heuristic; 5 / 20).
       CASE
         WHEN p.min_idle_instances IS NULL THEN 'NOT_ASSESSED'
         WHEN p.min_idle_instances >= 20 THEN 'CRITICAL'
         WHEN p.min_idle_instances >= 5 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY instance_pool_id ORDER BY change_time DESC) AS rn
  FROM `system`.`compute`.`instance_pools`
) p
LEFT JOIN cost_rollup cr
  ON cr.workspace_id = p.workspace_id
 AND cr.instance_pool_id = p.instance_pool_id
WHERE p.rn = 1 AND p.delete_time IS NULL
ORDER BY p.min_idle_instances DESC
) q
