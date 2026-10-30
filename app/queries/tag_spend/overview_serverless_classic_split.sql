-- overview_serverless_classic_split over the dollars a tag filter keeps (app/core/tag_spend.py).
WITH tagged AS (__TAGGED__),
snap AS (SELECT max(as_of_date) AS d FROM tags.cost_day),
-- Serverless the way the check reads it: the flag, a SERVERLESS SKU, or a serverless-only product.
srv AS (
    SELECT t.*, (COALESCE(t.is_serverless, FALSE) OR upper(t.sku_name) LIKE '%SERVERLESS%'
                 OR t.billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                 'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')) AS on_serverless
    FROM tagged t
)
SELECT __W__ AS window_days, t.workspace_id,
       SUM(CASE WHEN t.on_serverless THEN t.quantity ELSE 0 END) AS serverless_net_dbus,
       SUM(CASE WHEN t.on_serverless THEN 0 ELSE t.quantity END) AS classic_net_dbus,
       ROUND(100.0 * SUM(CASE WHEN t.on_serverless THEN t.quantity ELSE 0 END) / NULLIF(SUM(t.quantity), 0), 1) AS serverless_pct,
       SUM(CASE WHEN t.is_photon THEN t.quantity ELSE 0 END) AS photon_net_dbus,
       ROUND(100.0 * SUM(CASE WHEN t.is_photon THEN t.quantity ELSE 0 END) / NULLIF(SUM(t.quantity), 0), 1) AS photon_pct,
       SUM(CASE WHEN t.is_perf_optimized THEN t.quantity ELSE 0 END) AS perf_optimized_net_dbus,
       SUM(CASE WHEN t.has_custom_tags THEN 0 ELSE t.quantity END) AS untagged_net_dbus,
       SUM(t.quantity) AS total_net_dbus
FROM srv t, snap a
WHERE t.usage_unit = 'DBU' AND t.usage_date >= a.d - __W__ AND t.usage_date < a.d
GROUP BY t.workspace_id
