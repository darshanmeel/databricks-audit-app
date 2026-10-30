-- generated from dbt/models/databricks_direct/cost/d_cost_by_compute_resource.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_by_compute_resource.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
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
       -- status: magnitude band on daily DBU cost per resource (field heuristic; 50 / 200) - size, not waste.
       CASE
         WHEN SUM(u.usage_quantity) IS NULL THEN 'NOT_ASSESSED'
         WHEN SUM(u.usage_quantity) >= 200 THEN 'CRITICAL'
         WHEN SUM(u.usage_quantity) >= 50 THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage` u
LEFT JOIN (
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
) pr
  ON  u.sku_name = pr.sku_name AND u.cloud = pr.cloud AND u.usage_unit = pr.usage_unit
  AND u.usage_end_time >= pr.price_start_time
  AND (pr.price_end_time IS NULL OR u.usage_end_time < pr.price_end_time)
LEFT JOIN (
  SELECT workspace_id, cluster_id, cluster_name
  FROM `system`.`compute`.`clusters`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
) lc
  ON lc.workspace_id = u.workspace_id AND lc.cluster_id = u.usage_metadata.cluster_id
LEFT JOIN (
  SELECT workspace_id, warehouse_id, warehouse_name
  FROM `system`.`compute`.`warehouses`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) = 1
) lw
  ON lw.workspace_id = u.workspace_id AND lw.warehouse_id = u.usage_metadata.warehouse_id
LEFT JOIN (
  SELECT workspace_id, instance_pool_id, instance_pool_name
  FROM `system`.`compute`.`instance_pools`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, instance_pool_id ORDER BY change_time DESC) = 1
) lp
  ON lp.workspace_id = u.workspace_id AND lp.instance_pool_id = u.usage_metadata.instance_pool_id
LEFT JOIN `system`.`access`.`workspaces_latest` w ON w.workspace_id = u.workspace_id
WHERE u.usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND u.usage_date < __AS_OF_DATE__
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
