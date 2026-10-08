-- query_id: cost_restatement_trust_metric
-- title: Billing restatement / trust metric
-- domain: cost   tier: lite
-- reads: system.billing.usage
-- requires: SELECT on system.billing; GA (system.billing.usage is generally available)
-- empty_if: ingestion_lag
-- params: :period_days (default 30) rolling window in days
-- confidence: confirmed
-- confidence_note: The record_type enum (ORIGINAL/RETRACTION/RESTATEMENT) and ingestion_date are documented system.billing.usage columns.
-- read_this: One row = a workspace + cloud's usage totals for the window, split by record_type. The columns that matter are net_usage_quantity (the true net, corrections already applied) versus original_usage_quantity/retracted_abs_quantity/restatement_usage_quantity (how much of that net was corrected after the fact) - this is a trust/QA check on the billing data itself, not a cost or waste signal.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: cost_totals_by_sku_day (for the same net-corrected totals broken out by SKU/workspace), cost_by_billing_origin_product (for the net totals by product line)
-- caveats: This produces the "% of usage later restated" trust metric (retracted_abs_quantity + restatement_usage_quantity against original_usage_quantity) and demonstrates that net_usage_quantity does not double-count corrections - it is not itself a cost or misconfiguration finding, so there is no healthy/investigate_if band. ingestion_date (distinct from usage_date) supports incremental loads and is the basis for a data-freshness/lag check: max_ingestion_date tells you how current your last load is. workspace_id is a genuine account-level usage row when NULL, never dropped.
SELECT workspace_id, cloud,
       SUM(usage_quantity) AS net_usage_quantity,
       SUM(CASE WHEN record_type = 'ORIGINAL'    THEN usage_quantity      ELSE 0 END) AS original_usage_quantity,
       SUM(CASE WHEN record_type = 'RETRACTION'  THEN ABS(usage_quantity) ELSE 0 END) AS retracted_abs_quantity,
       SUM(CASE WHEN record_type = 'RESTATEMENT' THEN usage_quantity      ELSE 0 END) AS restatement_usage_quantity,
       MAX(ingestion_date) AS max_ingestion_date
FROM system.billing.usage
WHERE usage_date >= dateadd(day, -:period_days, current_date())
  AND usage_date < current_date()
GROUP BY workspace_id, cloud
ORDER BY cloud
