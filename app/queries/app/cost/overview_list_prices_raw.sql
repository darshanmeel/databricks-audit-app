-- query_id: overview_list_prices_raw
-- title: Raw list prices - safe basis for in-engine dollarization
-- domain: cost   tier: lite
-- reads: system.billing.list_prices
-- requires: SELECT on system.billing; GA
-- params: none (config snapshot, no time window)
-- confidence: needs_confirmation
-- confidence_note: Ported from the reference overview set, not yet run live. pricing.default
--   and pricing.effective_list are typed as STRUCT in Databricks but cast to STRING for stable
--   JSON serialization across system table versions.
-- read_this: One row = a SKU + cloud + currency code's current or expired list price, with
--   the full pricing struct serialized to JSON. Use this to dollarize usage_quantity in
--   in-engine SQL when the pricing.effective_list.default path is unavailable or unverified.
-- healthy: n/a - inventory
-- investigate_if: n/a - inventory
-- actions: n/a - inventory (reference/join input)
-- next: overview_spend_estimate (which uses the effective_list.default path, DEC-66.1's one
--   basis), cost_dollarized_by_sku_day (for list-priced usage by day)
-- caveats: pricing_default is the pre-promotion list rate; pricing_effective_list_json parses to
--   the effective (post-promotion) list rate DEC-66.1 uses as this app's one dollar basis.
--   Neither is negotiated or account-specific - no negotiated-rate source exists anywhere in
--   system.billing. effective_list / promotional are serialized to JSON strings so collection
--   survives struct drift; parse defensively. price_end_time NULL = current price.
--   pricing.default is typed STRING in the doc - confirm numeric castability before arithmetic.
--   This is the SAFE companion to overview_spend_estimate. Account-wide inventory (no
--   workspace_id). Ported from the MIT-licensed reference (c) 2026 darshanmeel.
SELECT
    price_start_time,
    price_end_time,
    account_id,
    sku_name,
    cloud,
    currency_code,
    usage_unit,
    CAST(pricing.default AS STRING) AS pricing_default,
    CAST(pricing.effective_list AS STRING) AS pricing_effective_list_json,
    CAST(pricing.promotional AS STRING) AS pricing_promotional_json
FROM system.billing.list_prices
