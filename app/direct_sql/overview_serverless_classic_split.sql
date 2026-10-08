-- generated from dbt/models/databricks_direct/cost/d_overview_serverless_classic_split.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/app/cost/overview_serverless_classic_split.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
WITH usage_rows AS (
    SELECT workspace_id, usage_quantity, product_features, custom_tags,
           CASE WHEN (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%'
               OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                             'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))
                THEN TRUE ELSE FALSE END AS on_serverless
    FROM `system`.`billing`.`usage`
    WHERE usage_unit = 'DBU'
      AND usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
      AND usage_date < __AS_OF_DATE__
)
SELECT
    workspace_id,
    SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END) AS serverless_net_dbus,
    SUM(CASE WHEN on_serverless THEN 0 ELSE usage_quantity END) AS classic_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN on_serverless THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS serverless_pct,
    SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END) AS photon_net_dbus,
    ROUND(100.0 * SUM(CASE WHEN product_features.is_photon = TRUE THEN usage_quantity ELSE 0 END)
          / NULLIF(SUM(usage_quantity), 0), 1) AS photon_pct,
    SUM(CASE WHEN product_features.performance_target IS NOT NULL THEN usage_quantity ELSE 0 END) AS perf_optimized_net_dbus,
    SUM(CASE WHEN cardinality(map_keys(custom_tags)) = 0 THEN usage_quantity ELSE 0 END) AS untagged_net_dbus,
    SUM(usage_quantity) AS total_net_dbus
FROM usage_rows
GROUP BY workspace_id
) q
