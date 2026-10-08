-- query_id: overview_serverless_classic_split
-- title: Serverless vs classic split - net DBUs and shares by workspace
-- domain: cost   tier: lite
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA
-- params: :period_days (default 30) rolling window in days
-- confidence: needs_confirmation
-- confidence_note: Ported from the reference overview set, not yet run live; confirm
--   SUM(usage_quantity) across all record_types matches the usage dashboard total for the same
--   window.
-- read_this: One row = a workspace's net DBU split between serverless and classic compute,
--   and the shares of Photon and performance-optimized SKUs. Use these to track serverless
--   adoption and Photon penetration by workspace.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: overview_spend_estimate (for the same workspace's spend estimate),
--   cost_premium_serverless_photon (if serverless or Photon SKUs dominate)
-- caveats: serverless = product_features.is_serverless, OR a SKU name with SERVERLESS in it, OR a
--   product that only runs on Databricks-managed compute (model serving, vector search, Genie, AI
--   functions, AI gateway, Agent Bricks, Lakebase, apps): Databricks leaves is_serverless false on
--   several of those. Everything else is classic; a null is_photon counts as non-Photon.
--   DBU-only (usage_unit='DBU'). The current day is excluded. Ported from the MIT-licensed
--   reference (c) 2026 darshanmeel.
WITH usage_rows AS (
    SELECT workspace_id, usage_quantity, product_features, custom_tags,
           CASE WHEN (product_features.is_serverless = TRUE OR upper(sku_name) LIKE '%SERVERLESS%'
               OR billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                             'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS'))
                THEN TRUE ELSE FALSE END AS on_serverless
    FROM system.billing.usage
    WHERE usage_unit = 'DBU'
      AND usage_date >= dateadd(day, -:period_days, current_date())
      AND usage_date < current_date()
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
