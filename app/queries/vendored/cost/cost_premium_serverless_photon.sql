-- query_id: cost_premium_serverless_photon
-- title: Usage by serverless / Photon / tier premium lever
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: product_features.{is_serverless, is_photon, jobs_tier, sql_tier, dlt_tier, performance_target} are documented system.billing.usage columns.
-- read_this: One row = a workspace + day + cloud + SKU + product's usage cut by serverless/Photon/tier choice. The columns that matter are is_serverless, is_photon, and the *_tier columns (which premium lever, if any, applied) against net_usage_quantity (how much usage ran through that lever).
-- healthy: The lever columns (is_serverless / is_photon / *_tier / performance_target) mostly null or false for a workload that has not opted into a premium path, with net_usage_quantity concentrated on the classic/base rows - a qualitative judgement call, since usage_unit is not restricted here and volumes are not comparable across different sku_name families.
-- investigate_if: A sku_name/product with a large, growing net_usage_quantity on rows where is_serverless/is_photon = true or a *_tier is a premium tier - compare it against that SAME sku_name's classic rows before assuming the premium is unjustified, since raw volumes are not comparable across different SKUs' units.
-- actions: 1) confirm the workload actually needs the premium tier (serverless start-up latency, Photon vectorization, DLT Advanced) rather than defaulting to it (free); 2) step a workload down to Standard tier or classic compute where the premium is not earning its keep (config); 3) if the premium genuinely pays for itself in latency/throughput, budget for it deliberately rather than discovering it after the fact (spend).
-- next: cost_by_compute_resource (to see which specific clusters/warehouses carry the premium usage), cost_serving_mode_by_endpoint (for the model-serving-specific premium/cost-mode cut)
-- caveats: product_features is sparse - subfields are null where the serverless/classic/Photon/tier choice does not apply; non-serverless workloads have performance_target = null. is_photon can be cross-checked via sku_name LIKE '%(PHOTON)%' as a string-pattern heuristic. Restricted to usage_unit = 'DBU', so net_usage_quantity is comparable across sku_name families within this result - there is still no single dollar threshold that is safe to apply uniformly here, so no automated status band is computed and rows are ordered for readability, not severity. Pair with list_prices to dollarize the premium delta yourself. workspace_id is added to the SELECT and GROUP BY so the app's workspace filter can narrow this check; it does not change any other column's meaning.
SELECT usage_date, cloud, workspace_id, sku_name, billing_origin_product,
       product_features.is_serverless      AS is_serverless,
       product_features.is_photon          AS is_photon,
       product_features.jobs_tier          AS jobs_tier,
       product_features.sql_tier           AS sql_tier,
       product_features.dlt_tier           AS dlt_tier,
       product_features.performance_target AS performance_target,
       SUM(usage_quantity) AS net_usage_quantity
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, sku_name, billing_origin_product,
         product_features.is_serverless, product_features.is_photon, product_features.jobs_tier,
         product_features.sql_tier, product_features.dlt_tier, product_features.performance_target
ORDER BY usage_date DESC, sku_name, cloud
