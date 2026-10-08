-- query_id: cost_chargeback_by_tag
-- title: DBU usage by custom tag (chargeback + coverage)
-- domain: cost   tier: standard
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: custom_tags is a documented map column on system.billing.usage.
-- read_this: One row = a day + workspace + product + tag key/value pair's DBU usage, including a row per tag_key = NULL where a resource carried no tags at all. The columns that matter are tag_key/tag_value (a real chargeback cut needs a key whose values actually vary) and net_usage_quantity - pair the NULL-tag rows against cost_totals_by_sku_day's total for the same window to get the untagged share.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the total DBU denominator to compute an untagged %), cost_usage_policy_coverage (for the serverless-specific coverage gap, tracked separately from tags)
-- caveats: The tag explode uses OUTER so rows with empty/NULL custom_tags still appear - that is what drives the "% untagged" calculation; pair with cost_totals_by_sku_day for the total. custom_tags keys vary per customer (it is a dynamic map) - do not hardcode key names when reading this. Tags originate from both compute-resource tags and serverless usage policies. Watch for the case where one umbrella tag key covers close to 100% of DBUs yet gives no team-level chargeback because it only ever takes a single value - a tag key is only chargeback-usable if it has more than one distinct tag_value in your results, so do not read "100% tagged" alone as "100% chargeback-ready"; count distinct tag_values per tag_key yourself. workspace_id lets chargeback be cut per workspace.
SELECT usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value,
       SUM(usage_quantity) AS net_usage_quantity,
       COUNT(*) AS record_count
FROM system.billing.usage
     LATERAL VIEW OUTER explode(custom_tags) t AS tag_key, tag_value
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY usage_date, cloud, workspace_id, billing_origin_product, tag_key, tag_value
ORDER BY usage_date DESC, workspace_id, tag_key, tag_value
