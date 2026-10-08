-- query_id: cost_usage_policy_coverage
-- title: Usage policy coverage for serverless spend
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: usage_metadata.{usage_policy_id, budget_policy_id} and product_features.is_serverless are documented system.billing.usage columns.
-- read_this: One row = a day + workspace + product's serverless usage cut by policy and tag coverage. The columns that matter are policy_coverage ('none' on a serverless row is the uncovered gap) and is_serverless (coverage is only meaningful for serverless spend) - tag_coverage is tracked separately, so a warehouse can be tagged yet still show 'none' on policy.
-- healthy: is_serverless = true rows show policy_coverage in ('usage_policy', 'budget_policy_legacy') - field heuristic.
-- investigate_if: is_serverless = true AND policy_coverage = 'none' (WARN) - field heuristic; the fix differs by product, an uncovered notebook/job needs a usage policy attached, an uncovered SQL warehouse needs a tag.
-- actions: 1) identify the workspace/product combination driving the uncovered DBUs from this result directly (free); 2) attach a usage policy to uncovered INTERACTIVE/JOBS serverless compute, or a tag to an uncovered SQL warehouse (config); 3) make policy/tag attachment a provisioning gate (for example via Terraform or a cluster policy) so new serverless usage cannot go uncovered (spend, in the sense of process investment).
-- next: cost_chargeback_by_tag (tag coverage is tracked separately from policy coverage here), cost_totals_by_sku_day (for the total DBU denominator)
-- caveats: usage_policy_id is current; budget_policy_id is deprecated ("use usage_policy_id instead") and is kept only as a fallback for older rows. Policy IDs populate only for serverless usage, so compute coverage is a % over serverless spend only (is_serverless = true) - non-serverless rows are not part of this coverage story. No system table lists policy definitions (name/owner) - that is Budget Policy API only. tag_coverage (whether custom_tags is non-empty) is tracked separately from policy_coverage on purpose: a warehouse can be tagged yet still show 'none' on policy, so do not read a tagged-but-policy-uncovered SQL row as "uncovered" in the tagging sense - the fix differs by product (notebook/job -> policy, warehouse -> tag). workspace_id lets the gap be closed workspace by workspace. Restricted to usage_unit = 'DBU', so net_usage_quantity is comparable across rows.
SELECT usage_date, cloud, workspace_id, billing_origin_product,
       product_features.is_serverless AS is_serverless,
       CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
            WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
            ELSE 'none' END AS policy_coverage,
       CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END AS tag_coverage,
       SUM(usage_quantity) AS net_usage_quantity,
       -- status: yes/no coverage-gap flag on serverless spend (field heuristic).
       CASE
         WHEN product_features.is_serverless IS NULL THEN 'NOT_ASSESSED'
         WHEN product_features.is_serverless = true
              AND (CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
                        WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
                        ELSE 'none' END) = 'none'
              THEN 'WARN'
         ELSE 'OK'
       END AS status
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
  AND usage_unit = 'DBU'
GROUP BY usage_date, cloud, workspace_id, billing_origin_product, product_features.is_serverless,
         CASE WHEN usage_metadata.usage_policy_id  IS NOT NULL THEN 'usage_policy'
              WHEN usage_metadata.budget_policy_id IS NOT NULL THEN 'budget_policy_legacy'
              ELSE 'none' END,
         CASE WHEN cardinality(map_keys(custom_tags)) > 0 THEN 'tagged' ELSE 'untagged' END
ORDER BY status DESC, net_usage_quantity DESC
