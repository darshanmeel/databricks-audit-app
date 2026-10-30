-- generated from dbt/models/databricks_direct/cost/d_cost_usage_policy_coverage.sql by tools/build_direct_sql.py; edit the query, never this file.
-- generated from app/queries/vendored/cost/cost_usage_policy_coverage.sql; fix the source query and regenerate, never edit this file.
SELECT __WINDOW_DAYS__ AS window_days, q.*
FROM (
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       -- serverless-only products carry no is_serverless (no classic choice), yet take usage policies
       COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) AS is_serverless,
       CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
            WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
            ELSE 'none' END AS policy_coverage,
       CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END AS tag_coverage,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: yes/no coverage-gap flag on serverless spend (field heuristic).
       CASE
         WHEN COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) IS NULL THEN 'NOT_ASSESSED'
         WHEN COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END) = true
              AND (CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
                        WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
                        ELSE 'none' END) = 'none'
              THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM `system`.`billing`.`usage`
WHERE usage_date >= dateadd(day, -__WINDOW_DAYS__, __AS_OF_DATE__)
  AND usage_date < __AS_OF_DATE__
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product,
         COALESCE(product_features.is_serverless,
                CASE WHEN billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'GENIE', 'AI_FUNCTIONS',
                                                     'AI_GATEWAY', 'AGENT_BRICKS', 'LAKEBASE', 'APPS')
                     THEN true END),
         CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
              WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
              ELSE 'none' END,
         CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END
ORDER BY status DESC, net_usage_quantity DESC
) q
